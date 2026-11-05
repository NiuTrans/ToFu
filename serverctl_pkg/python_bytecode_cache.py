"""Host-local bytecode cache for managed server workers.

Responsibility: select and maintain one reconstructible ``PYTHONPYCACHEPREFIX``
when the application checkout is on a network/userspace filesystem and the
manager can prove that the cache is local.  The cache is isolated by project
and interpreter identity and never owns source or durable application data.
Commands the worker runs inherit the prefix, so their bytecode stays off the
network volume too.

Retention keeps whatever can still be used and has no size quota.  It removes
bytecode whose source directory was provably removed (a detached sweep, see
:func:`request_bytecode_sweep`), namespaces whose project or interpreter was
provably removed or that nothing used for a week, and, only while the cache
volume is below the shared free-space reserve, other unused namespaces oldest
first.  The namespace being activated and any namespace in use are never
removed.

Entry points: :func:`prepare_server_python_cache`,
:func:`reacquire_server_python_cache_lease` and :func:`request_bytecode_sweep`
for the lifecycle manager.  :func:`reclaim_namespaces`,
:func:`record_namespace_identity` and :func:`request_bytecode_sweep` are also
the retention primitives of ``lib.project_mod.python_bytecode_cache``, and
``python -m serverctl_pkg.python_bytecode_cache sweep`` is the detached
sweeper.  This module intentionally uses only the Python standard library
(and the shared stdlib helpers in :mod:`serverctl_pkg.local_placement`) so
lifecycle recovery remains available when application dependencies are
broken.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

from serverctl_pkg.local_placement import Volume, describe_volume, has_free_space

__all__ = [
    'IDENTITY_NAME', 'RUN_NAMESPACE_NAME', 'ServerPythonCacheActivation',
    'prepare_server_python_cache', 'reacquire_server_python_cache_lease',
    'reclaim_namespaces', 'record_namespace_identity', 'request_bytecode_sweep',
    'sweep_dead_bytecode', 'volume_has_room',
]


_TTL_SECONDS = 7 * 24 * 60 * 60
# Dead bytecode accumulates as worktrees and scratch trees are removed
# (measured 2026-10-25: 722 MiB of 1.8 GiB within a day and a half of agent
# work); one sweep costs a few seconds of directory lookups.
SWEEP_INTERVAL_SECONDS = 6 * 60 * 60
IDENTITY_NAME = '.identity.json'
SWEEP_MARKER_NAME = '.last-swept'
SWEEP_LOCK_NAME = '.sweep.lock'
_NAMESPACE_NAME = re.compile(r'^ns-[0-9a-f]{16}-[0-9a-f]{16}$')
# Run-command namespaces (lib.project_mod.python_bytecode_cache) add an owner.
RUN_NAMESPACE_NAME = re.compile(r'^ns-[0-9a-f]{12}-[0-9a-f]{16}-[0-9a-f]{16}$')
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_OFF_VALUES = frozenset({'0', 'false', 'no', 'off', 'disabled'})
_ON_VALUES = frozenset({'1', 'true', 'yes', 'on', 'enabled', 'force'})
_REMOTE_SOURCE_CLASSES = frozenset({
    'network-filesystem', 'userspace-filesystem',
})
_LOCAL_CACHE_CLASSES = frozenset({
    'local-block', 'container-overlay', 'memory-filesystem',
})


@dataclass(frozen=True, slots=True)
class ServerPythonCacheActivation:
    """One cache decision plus an optional child-owned namespace lease."""

    selected: bool
    managed: bool
    reason: str
    mode: str
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


def _write_private_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(temporary, flags, 0o600), 'w', encoding='utf-8') as handle:
        json.dump(value, handle, sort_keys=True)
    os.replace(temporary, path)


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


# ── retention: what nothing can use any more ─────────────────────────────


def _parent_visible(path: str) -> bool:
    """Whether ``path``'s parent is reachable and can witness a removal.

    An unmounted volume leaves an empty mount-point directory behind, so an
    empty parent proves nothing about its former children.
    """
    parent = os.path.dirname(os.path.normpath(path)) or os.sep
    try:
        with os.scandir(parent) as entries:
            return next(entries, None) is not None
    except OSError:
        return False


def _source_state(path: str, *, directory: bool = False) -> str:
    """``present``, ``removed`` (provably) or ``unknown`` for ``path``.

    Only "does not exist" seen from a visible parent proves removal; an I/O
    error or an unreachable volume never does. With ``directory``, a
    non-directory now at ``path`` means the directory was replaced.
    """
    try:
        metadata = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return 'removed' if _parent_visible(path) else 'unknown'
    except OSError:
        return 'unknown'
    if directory and not stat.S_ISDIR(metadata.st_mode):
        return 'removed'
    return 'present'


def record_namespace_identity(namespace: str | os.PathLike[str], paths: Iterable[str]) -> None:
    """Record the paths whose removal leaves ``namespace`` unusable."""
    _write_private_json(Path(namespace) / IDENTITY_NAME,
                        {'paths': [os.path.realpath(path) for path in paths]})


def _identity_removed(namespace: Path) -> bool:
    try:
        record = json.loads((namespace / IDENTITY_NAME).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    paths = record.get('paths') if isinstance(record, dict) else None
    return isinstance(paths, list) and any(
        isinstance(path, str) and os.path.isabs(path) and _source_state(path) == 'removed'
        for path in paths)


def _last_used(namespace: Path) -> float:
    for candidate in (namespace / '.last-used', namespace):
        try:
            return candidate.lstat().st_mtime
        except OSError:
            continue
    return 0.0


def _safe_remove_namespace(
    root: Path,
    namespace: Path,
    pattern: re.Pattern[str] = _NAMESPACE_NAME,
) -> bool:
    descriptor = None
    try:
        if namespace.parent != root or not pattern.fullmatch(namespace.name):
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


def reclaim_namespaces(
    root: str | os.PathLike[str],
    *,
    pattern: re.Pattern[str],
    ttl_seconds: float,
    keep: Iterable[str] = (),
    has_space: Callable[[], bool] | None = None,
    now: float | None = None,
) -> int:
    """Remove namespaces nothing can use; returns how many were removed.

    Names in ``keep`` and namespaces in use (a live shared lease) survive.
    Another namespace goes when unused for ``ttl_seconds``, when a path its
    identity record names was provably removed, or, oldest first, while
    ``has_space`` reports the cache volume below its free-space reserve.
    Cost is one directory listing plus a few lookups per namespace.
    """
    root_path = Path(root)
    kept = set(keep)
    current = time.time() if now is None else now
    candidates = []
    try:
        with os.scandir(root_path) as entries:
            for entry in entries:
                if entry.name in kept or not pattern.fullmatch(entry.name):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        candidates.append(Path(entry.path))
                except OSError:
                    continue
    except OSError:
        return 0
    removed = 0
    remaining = []
    for namespace in candidates:
        last_used = _last_used(namespace)
        if ((current - last_used > ttl_seconds or _identity_removed(namespace))
                and _safe_remove_namespace(root_path, namespace, pattern)):
            removed += 1
        else:
            remaining.append((last_used, namespace.name, namespace))
    if has_space is not None:
        for _last_used_at, _name, namespace in sorted(remaining):
            if has_space():
                break
            removed += _safe_remove_namespace(root_path, namespace, pattern)
    return removed


def _child_directories(path: Path) -> list[Path]:
    children = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        children.append(Path(entry.path))
                except OSError:
                    continue
    except OSError:
        return []
    return children


def sweep_dead_bytecode(prefix: str | os.PathLike[str]) -> dict[str, int]:
    """Remove mirrored directories whose source directory was removed.

    ``PYTHONPYCACHEPREFIX`` mirrors each source directory's absolute path, so
    bytecode below the mirror of a removed directory can never load again.
    The walk is top-down and never descends into a removed subtree. It is
    safe beside running interpreters: a racing writer at worst recompiles.
    """
    root = Path(prefix)
    checked = removed = 0
    pending = [root]
    while pending:
        mirror = pending.pop()
        for child in _child_directories(mirror):
            checked += 1
            state = _source_state(os.sep + os.path.relpath(child, root), directory=True)
            if state == 'removed':
                shutil.rmtree(child, ignore_errors=True)
                removed += not os.path.lexists(child)
            elif state == 'present':
                pending.append(child)
    return {'directoriesChecked': checked, 'subtreesRemoved': removed}


def volume_has_room(path: str | os.PathLike[str]) -> bool:
    """The shared placement reserve, probed at call time."""
    return has_free_space(path, disk_usage=shutil.disk_usage)


def _sweepable(cache_root: Path, namespace: str) -> bool:
    return bool((_NAMESPACE_NAME.fullmatch(namespace) or RUN_NAMESPACE_NAME.fullmatch(namespace))
                and _is_private_directory(cache_root)
                and _is_private_directory(cache_root / namespace))


def request_bytecode_sweep(
    cache_root: str | os.PathLike[str],
    namespace: str,
    *,
    interval_seconds: float = SWEEP_INTERVAL_SECONDS,
    python: str | None = None,
) -> bool:
    """Start one detached, low-priority sweep of ``namespace`` when due.

    Cheap to call often: it returns at once while the last sweep is recent or
    one is running, and it never waits for the sweep. Returns whether a sweep
    was started.
    """
    root = Path(cache_root)
    if not _sweepable(root, namespace):
        return False
    directory = root / namespace
    marker = directory / SWEEP_MARKER_NAME
    try:
        if time.time() - marker.lstat().st_mtime < interval_seconds:
            return False
    except FileNotFoundError:
        pass
    except OSError:
        return False
    probe = _open_lock(directory / SWEEP_LOCK_NAME, exclusive=True)
    if probe is None:
        return False
    _close_lock(probe)
    try:
        _touch_private_marker(marker)
    except OSError:
        return False
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('PYTHON')}
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    argv = [python or sys.executable, '-s', '-m', 'serverctl_pkg.python_bytecode_cache',
            'sweep', '--root', str(root), '--namespace', namespace]
    try:
        subprocess.Popen(
            argv, cwd=str(_PACKAGE_ROOT), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True, env=environment)
    except OSError:
        return False
    return True


# ── server activation ────────────────────────────────────────────────────


_EMPTY_MOUNT = Volume()


def _disabled(
    reason: str,
    *,
    mode: str,
    source: Volume = _EMPTY_MOUNT,
    cache: Volume = _EMPTY_MOUNT,
    selected: bool = False,
) -> ServerPythonCacheActivation:
    return ServerPythonCacheActivation(
        selected=selected,
        managed=False,
        reason=reason,
        mode=mode,
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
    dont_write = str(environment.get(
        'PYTHONDONTWRITEBYTECODE', '')).strip()
    if dont_write:
        return _disabled('python-bytecode-writes-disabled', mode=mode)
    if str(environment.get('PYTHONPYCACHEPREFIX', '')).strip():
        return _disabled('operator-python-prefix', mode=mode, selected=True)
    if mode == 'off':
        return _disabled('disabled', mode=mode)

    cache_root = _resolve_cache_root(project_path, environment)
    if cache_root is None:
        return _disabled('unsafe-cache-root', mode=mode)
    source_mount = describe_volume(project_path, mountinfo_text=mountinfo_text)
    cache_mount = describe_volume(
        _nearest_existing_path(cache_root), mountinfo_text=mountinfo_text)
    if cache_mount.storage_class not in _LOCAL_CACHE_CLASSES:
        return _disabled(
            'cache-not-local', mode=mode, source=source_mount, cache=cache_mount)
    if (
        mode == 'auto'
        and source_mount.storage_class not in _REMOTE_SOURCE_CLASSES
    ):
        return _disabled(
            'source-not-remote', mode=mode, source=source_mount, cache=cache_mount)

    namespace_name = _namespace_name(project_path, python_executable)
    if namespace_name is None:
        return _disabled(
            'identity-unavailable', mode=mode, source=source_mount, cache=cache_mount)
    namespace = cache_root / namespace_name
    maintenance_lock = None
    namespace_lock = None
    try:
        _ensure_private_directory(cache_root)
        maintenance_lock = _open_lock(
            cache_root / '.maintenance.lock', exclusive=True)
        if maintenance_lock is None:
            return _disabled(
                'maintenance-busy', mode=mode, source=source_mount, cache=cache_mount)
        reclaim_namespaces(
            cache_root, pattern=_NAMESPACE_NAME, ttl_seconds=_TTL_SECONDS,
            keep={namespace_name}, has_space=lambda: volume_has_room(cache_root))
        if not volume_has_room(cache_root):
            return _disabled(
                'insufficient-cache-space', mode=mode, source=source_mount,
                cache=cache_mount)
        _ensure_private_directory(namespace)
        namespace_lock = _open_lock(
            namespace / '.active.lock', exclusive=False)
        if namespace_lock is None:
            return _disabled(
                'namespace-busy', mode=mode, source=source_mount, cache=cache_mount)
        prefix = namespace / 'pycache'
        _ensure_private_directory(prefix)
        _touch_private_marker(namespace / '.last-used')
        record_namespace_identity(namespace, (project_path, python_executable))
    except (OSError, RuntimeError, ValueError):
        _close_lock(namespace_lock)
        return _disabled(
            'cache-setup-failed', mode=mode, source=source_mount, cache=cache_mount)
    finally:
        _close_lock(maintenance_lock)

    return ServerPythonCacheActivation(
        selected=True,
        managed=True,
        reason='remote-source-local-cache' if mode == 'auto' else 'forced',
        mode=mode,
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
        or describe_volume(
            root, mountinfo_text=mountinfo_text,
        ).storage_class not in _LOCAL_CACHE_CLASSES
    ):
        return None
    return _open_lock(candidate / '.active.lock', exclusive=False)


# ── detached sweeper ─────────────────────────────────────────────────────


def _sweep_main(cache_root: str, namespace: str) -> int:
    try:
        os.nice(10)
    except OSError:
        pass
    root = Path(cache_root)
    if not _sweepable(root, namespace):
        return 2
    directory = root / namespace
    lock = _open_lock(directory / SWEEP_LOCK_NAME, exclusive=True)
    if lock is None:
        return 0
    # A shared lease keeps launch-time reclamation from removing the
    # namespace underneath the walk.
    lease = _open_lock(directory / '.active.lock', exclusive=False)
    try:
        if lease is None:
            return 0
        started = time.monotonic()
        result: dict[str, object] = dict(sweep_dead_bytecode(directory / 'pycache'))
        result.update(seconds=round(time.monotonic() - started, 1), finishedAt=time.time())
        try:
            _write_private_json(directory / SWEEP_MARKER_NAME, result)
        except OSError:
            pass
    finally:
        _close_lock(lease)
        _close_lock(lock)
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog='python -m serverctl_pkg.python_bytecode_cache')
    commands = parser.add_subparsers(dest='command', required=True)
    sweep = commands.add_parser(
        'sweep', help='remove bytecode whose source directories were removed')
    sweep.add_argument('--root', required=True)
    sweep.add_argument('--namespace', required=True)
    arguments = parser.parse_args(argv)
    return _sweep_main(arguments.root, arguments.namespace)


if __name__ == '__main__':
    raise SystemExit(main())
