"""Engine status and storage-path discovery for the database panel.

Default status reads the running supervisor and cached read-only mount/free
space evidence, never launch admission or write-capability probes. Explicit
path discovery does real fsync/WAL round trips, cached for ten minutes with
singleflight admission. Full binary verification remains at switch/launch.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any

from lib.log import get_logger
from lib.runtime_paths import data_root

from lib.storage_sidecar.projection_paths import (
    MAX_DIRECTORY_LENGTH as MAX_DIRECTORY_LENGTH,
    SearchProjectionEnvironmentOverrideError as SearchProjectionEnvironmentOverrideError,
    resolve_search_projection_paths as resolve_search_projection_paths,
    save_search_projection_directory as save_search_projection_directory,
)
from lib.storage_sidecar.native_fastpath_settings import NativeFastpathSettingsError as NativeFastpathSettingsError

logger = get_logger(__name__)

_PROBE_CACHE_TTL_S = 600.0
_STATUS_FS_CACHE_TTL_S = 60.0
_RECOMMEND_MIN_FREE_BYTES = 2 * 1024 ** 3
_DIR_BYTES_WALK_LIMIT = 200_000
# The authority tree can hold ~1M block files (2026-09-22 production); on a
# network/FUSE mount every stat costs ~0.5 ms, so an uncached full walk held
# /api/v1/database/status open for 105+ s per call. Bound one walk by wall
# clock and cache the completed total — a settings-panel size display never
# needs to be fresher than this.
_DIR_BYTES_WALK_DEADLINE_S = 20.0
_DIR_BYTES_CACHE_TTL_S = 600.0

_probe_cache: dict[str, Any] = {'at': 0.0, 'result': None}
_fs_cache: dict[str, Any] = {'at': 0.0, 'path': '', 'report': None}
_dir_bytes_cache: dict[str, Any] = {}
_dir_bytes_inflight: set[str] = set()
_cache_lock = threading.Lock()

_fs_report_lock = threading.Lock()
_discovery_lock = threading.Lock()


def configured_backend() -> str:
    """Return the launch-selected backend ('sqlite' | 'tofudb' | 'unknown')."""
    try:
        from runtime_guards import load_deployment_configuration

        return load_deployment_configuration().storage_backend
    except Exception as _e:  # noqa: BLE001 - status must degrade, never 500
        logger.debug('probe configured backend failed: %s', _e)
        return 'unknown'


def deployment_mode() -> str:
    try:
        from runtime_guards import resolve_deployment_mode

        return resolve_deployment_mode()
    except Exception as _e:  # noqa: BLE001
        logger.debug('probe deployment mode failed: %s', _e)
        return 'unknown'


def resolve_tofudb_binary() -> str | None:
    """Mirror the supervisor's resolution order; None when uninstalled."""
    try:
        from lib.storage.supervisor import _resolve_tofudb_binary

        return _resolve_tofudb_binary()
    except Exception as _e:  # noqa: BLE001 - absence is a normal state
        logger.debug('resolve tofu-db binary failed: %s', _e)
        return None


def _walk_dir_bytes(root: Path) -> int | None:
    """Bounded full walk; returns the byte total or None on unreadable root.

    Runs off the request path (see ``_dir_bytes_bounded``). The wall-clock
    deadline bounds one walk's cost on a network/FUSE mount; the limit bounds
    total work for a runaway tree. Neither affects correctness — a size
    display tolerates a partial-but-truthful total.
    """
    total = 0
    walked = 0
    deadline = time.monotonic() + _DIR_BYTES_WALK_DEADLINE_S
    try:
        stack = [root]
        while stack:
            current = stack.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    walked += 1
                    if walked > _DIR_BYTES_WALK_LIMIT:
                        return total
                    if walked & 0x3FF == 0 and time.monotonic() > deadline:
                        return total
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
    except OSError:
        return None
    return total


def _dir_bytes_bounded(root: Path) -> int | None:
    """Serve the cached byte total instantly; refresh the walk in background.

    A status endpoint must never do real filesystem IO on the request path:
    the authority tree holds ~1M block files on a network mount, so a walk
    held ``/api/v4/database/status`` open 20+s per call (2026-09-24 SLOW log),
    competing with the storage engine for the same starved IO. Return the
    freshest completed total immediately — even when stale — and kick one
    background walk to refresh the TTL window. On a true cold start (no walk
    has ever completed) there is no total to serve; return the in-progress
    marker ``None`` rather than block, and the background walk fills it in.
    """
    cache_key = str(root)
    with _cache_lock:
        cached = _dir_bytes_cache.get(cache_key)
        fresh = (cached is not None
                 and time.monotonic() - cached['at'] < _DIR_BYTES_CACHE_TTL_S)
        if fresh:
            return cached['total']
        if cache_key in _dir_bytes_inflight:
            # A refresh is already walking; serve what we have (possibly the
            # stale total or None) instead of piling a second walk onto IO.
            return cached['total'] if cached is not None else None
        _dir_bytes_inflight.add(cache_key)

    def _refresh() -> None:
        try:
            total = _walk_dir_bytes(root)
        except Exception as _e:  # noqa: BLE001 - status must degrade
            logger.debug('background dir-bytes walk failed: %s', _e)
            total = None
        with _cache_lock:
            _dir_bytes_inflight.discard(cache_key)
            if total is not None:
                _dir_bytes_cache[cache_key] = {
                    'at': time.monotonic(), 'total': total}

    threading.Thread(
        target=_refresh, name='dir-bytes-walk', daemon=True).start()
    return cached['total'] if cached is not None else None


def _fs_report(path: Path) -> dict[str, Any]:
    with _cache_lock:
        cached = _fs_cache
        if (cached['report'] is not None and cached['path'] == str(path)
                and time.monotonic() - cached['at'] < _STATUS_FS_CACHE_TTL_S):
            return dict(cached['report'])
    # No worker per request and no duplicated cold mount/statvfs reads.
    if not _fs_report_lock.acquire(blocking=False):
        with _cache_lock:
            return (dict(_fs_cache['report'])
                    if _fs_cache['path'] == str(path) and _fs_cache['report']
                    else {})
    try:
        from lib.storage_sidecar.storage_capabilities import describe_mount
        import shutil

        mount = describe_mount(path)
        try:
            free_bytes = shutil.disk_usage(path).free
        except OSError:
            free_bytes = None
        report = {
            'filesystem_type': mount.filesystem_type,
            'storage_class': mount.storage_class,
            'persistence': mount.persistence,
            'free_bytes': free_bytes,
            'probe_latency_ms': None,
        }
        with _cache_lock:
            _fs_cache.update(
                {'at': time.monotonic(), 'path': str(path), 'report': dict(report)})
        return report
    except Exception as exc:  # noqa: BLE001 - display evidence is best effort
        logger.debug('read filesystem display failed: %s', exc)
        return {}
    finally:
        _fs_report_lock.release()


def tofudb_authority_has_data(path: Path) -> bool:
    """True only when the authority holds user content.

    ``tofu-db init`` creates LOCK/CONTROL, a zero-byte ``active.wal`` and
    empty ``blocks/`` / ``payload-segments/`` directories.  Content lands in
    the WAL first and is packed into blocks later, so the witnesses are: a
    non-empty active.wal, or any file inside either content store.
    """
    try:
        if not path.is_dir():
            return False
        wal = path / 'active.wal'
        if wal.is_file() and wal.stat().st_size > 0:
            return True
        for store in ('blocks', 'payload-segments'):
            store_dir = path / store
            if store_dir.is_dir():
                for _root, _dirs, files in os.walk(store_dir):
                    if files:
                        return True
        return False
    except OSError:
        return True  # unreadable → treat as real data, never migrate over it


def runtime_status() -> dict[str, Any]:
    """Read supervision once; failure is unknown, not proof of absence."""
    try:
        from lib.storage.service import storage_status

        runtime = storage_status()
    except Exception as exc:  # noqa: BLE001
        logger.debug('probe engine status failed: %s', exc)
        runtime = {'ready': False, 'state': 'unknown',
                   'last_error': f'{type(exc).__name__}'}

    return runtime


def _status_has_data(path: Path) -> bool | None:
    """Cheap positive/absent witness; existing native trees require full scan."""
    try:
        if not path.exists():
            return False
        if path.is_file():
            return path.stat().st_size > 0
        wal = path / 'active.wal'
        if wal.is_file() and wal.stat().st_size > 0:
            return True
    except OSError:
        return None
    return None


def engine_status(runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    """Light display snapshot; next-launch admission is deliberately unknown."""
    backend = configured_backend()
    runtime = runtime_status() if runtime is None else runtime
    if runtime.get('ready') and runtime.get('backend') in ('sqlite', 'tofudb', 'postgres'):
        backend = str(runtime['backend'])
    root = Path(data_root())
    sqlite_db = root / 'tofu.db'
    tofudb_dir = root / 'tofudb-authority'
    native_fastpath = runtime.get('native_fastpath') if backend == 'tofudb' else None
    if backend == 'tofudb':
        authority_path = (Path(native_fastpath['localAuthority'])
                          if isinstance(native_fastpath, dict) else tofudb_dir)
    else:
        authority_path = (Path(str(runtime['authority_path']))
                          if runtime.get('ready') and runtime.get('backend') == 'sqlite'
                          and runtime.get('authority_path') else sqlite_db)
    data_bytes = None
    if authority_path.is_file():
        try:
            data_bytes = authority_path.stat().st_size
            for suffix in ('-wal', '-shm'):
                side = Path(str(authority_path) + suffix)
                if side.is_file():
                    data_bytes += side.stat().st_size
        except OSError:
            data_bytes = None
    elif authority_path.is_dir():
        data_bytes = _dir_bytes_bounded(authority_path)
    fs = _fs_report(root)
    native_running = backend == 'tofudb' and bool(runtime.get('ready'))
    return {
        'backend': backend,
        'deploymentMode': deployment_mode(),
        'ready': bool(runtime.get('ready')),
        'state': str(runtime.get('state') or 'unknown'),
        'lastError': str(runtime.get('last_error') or ''),
        'dataRoot': str(root),
        'authorityPath': str(authority_path),
        'nativeFastpath': native_fastpath,
        'dataBytes': data_bytes,
        'filesystem': {
            'type': fs.get('filesystem_type'),
            'class': fs.get('storage_class'),
            'persistence': fs.get('persistence'),
            'freeBytes': fs.get('free_bytes'),
            'probeLatencyMs': fs.get('probe_latency_ms'),

            'evidence': 'read-only',
        },
        'engines': {
            # No direct driver imports here: the storage boundary owns every
            # database touch; availability is a pure filesystem witness.
            'sqlite': {
                'available': True,
                'availabilityState': 'supported',
                'launchVerification': 'not-checked',
                'hasData': _status_has_data(sqlite_db),
            },
            'tofudb': {
                'available': True if native_running else None,
                'availabilityState': 'running' if native_running else 'unknown',
                'launchVerification': 'not-checked',
                'binaryPath': '',
                'hasData': _status_has_data(tofudb_dir),
            },
        },
    }


def search_projection_state(backend: str, runtime: dict[str, Any] | None = None
                            ) -> dict[str, Any] | None:
    """Actual native launch path versus the effective next-launch preference.

    No fallback to environment/settings for ``directory``: a stopped or older
    supervisor with no launch witness must not claim a projection is active.
    Base directories are user configuration; only effective namespaced paths
    may be sent to the native engine. This read never creates directories.
    """
    if backend != 'tofudb':
        return None
    from lib.storage.service import storage_status

    paths = resolve_search_projection_paths(Path(data_root()))
    runtime = storage_status() if runtime is None else runtime
    active = str(runtime.get('search_projection_directory') or '')
    pending = str(paths.directory)
    return {
        'required': True,
        'directory': active,
        'configuredDirectory': paths.configured_directory,
        'defaultDirectory': str(paths.default_directory),
        'pendingDirectory': pending,
        'restartRequired': active != pending,
    }


def configure_search_projection_directory(directory: str) -> dict[str, Any]:
    """Panel mutation boundary; storage owns validation and atomic persistence."""
    save_search_projection_directory(Path(data_root()), directory)
    return search_projection_state('tofudb')


def fastpath_state(backend: str, runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    """Configured next launch versus readiness-witnessed live acceleration."""
    if runtime is None:
        from lib.storage.service import storage_status

        runtime = storage_status()
    if backend == 'tofudb':
        from lib.storage_sidecar.native_fastpath_settings import native_fastpath_state

        return native_fastpath_state(Path(data_root()), runtime)
    state = {
        'supported': backend == 'sqlite',
        'kind': 'sqlite' if backend == 'sqlite' else 'unavailable',
        'mode': 'unavailable', 'enabled': False, 'directory': '',
        'active': None, 'activeDirectory': '', 'restartRequired': False,
        'locked': False, 'lockReason': '', 'authorityPath': '', 'shadowPath': '',
    }
    if backend != 'sqlite':
        return state
    mode = os.environ.get('TOFU_STORAGE_FASTPATH', 'off').strip() or 'off'
    state.update(mode=mode, enabled=mode in ('auto', 'required'),
                 directory=os.environ.get('TOFU_STORAGE_FASTPATH_DIR', '').strip())
    # Older supervisors expose no SQLite launch witness. Unknown is not off.
    if runtime.get('ready') and runtime.get('backend') == backend:
        active = runtime.get('fastpath_active')
        if isinstance(active, bool):
            authority = str(runtime.get('authority_path') or '')
            state.update(active=active, activeDirectory=authority,
                         authorityPath=authority,
                         shadowPath=str(runtime.get('shadow_dir') or ''),
                         restartRequired=(state['enabled'] != active))
    return state


def configure_native_fastpath(enabled: bool, directory: str) -> dict[str, Any]:
    """Delegate next-launch admission to the storage lifecycle owner."""
    from lib.database_panel.switch import env_file_path
    from lib.storage.service import storage_status
    from lib.storage_sidecar.native_fastpath_settings import save_native_fastpath_settings

    return save_native_fastpath_settings(
        Path(data_root()), env_file_path(), enabled, directory, storage_status())


def _candidate_paths() -> list[Path]:
    candidates: list[Path] = [data_root(), Path(tempfile.gettempdir())]
    dev_shm = Path('/dev/shm')
    if dev_shm.is_dir():
        candidates.append(dev_shm)
    xdg = os.environ.get('XDG_RUNTIME_DIR', '').strip()
    if xdg:
        candidates.append(Path(xdg))
    try:
        candidates.append(Path.home() / '.cache')
    except RuntimeError:
        pass
    return candidates


def _is_volatile(path: Path, storage_class: str) -> bool:
    if storage_class == 'memory-filesystem':
        return True
    resolved = os.path.realpath(str(path))
    ephemeral_roots = [os.path.realpath(tempfile.gettempdir())]
    xdg = os.environ.get('XDG_RUNTIME_DIR', '').strip()
    if xdg:
        ephemeral_roots.append(os.path.realpath(xdg))
    return any(resolved == root or resolved.startswith(root + os.sep)
               for root in ephemeral_roots)


def probe_single_path(path: Path, *, create_directory: bool = False
                      ) -> dict[str, Any]:
    """Bounded capability probe of one explicit path (fast-path admission)."""
    from lib.storage_sidecar.storage_capabilities import probe_storage_path

    report = probe_storage_path(path, create_directory=create_directory)
    return report.as_dict()


def discover_paths(*, refresh: bool = False) -> dict[str, Any]:
    """Explicit discovery, one scan in flight; concurrent callers never queue."""
    if not _discovery_lock.acquire(blocking=False):
        with _cache_lock:
            result = dict(_probe_cache['result'] or {
                'probedAt': None, 'candidates': [], 'recommendedPath': ''})
        return {**result, 'inProgress': True}
    try:
        return _discover_paths(refresh=refresh)
    finally:
        _discovery_lock.release()


def _discover_paths(*, refresh: bool = False) -> dict[str, Any]:
    """Probe candidate locations and rank a recommended faster path."""
    with _cache_lock:
        if (not refresh and _probe_cache['result'] is not None
                and time.monotonic() - _probe_cache['at'] < _PROBE_CACHE_TTL_S):
            return dict(_probe_cache['result'])

    from lib.storage_sidecar.storage_capabilities import probe_storage_path

    current_root = os.path.realpath(str(data_root()))
    seen: set[str] = set()
    entries: list[dict[str, Any]] = []
    for candidate in _candidate_paths():
        resolved = os.path.realpath(str(candidate))
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            report = probe_storage_path(candidate)
        except Exception as exc:  # noqa: BLE001
            entries.append({
                'path': resolved, 'writable': False,
                'storageClass': 'unknown', 'persistence': 'unknown',
                'freeBytes': None, 'latencyMs': None, 'volatile': False,
                'isCurrent': resolved == current_root,
                'limitations': [f'probe:{type(exc).__name__}'],
            })
            continue
        storage_class = str(report.storage_class)
        entries.append({
            'path': resolved,
            'writable': bool(report.writable),
            'storageClass': storage_class,
            'filesystemType': str(report.filesystem_type),
            'persistence': str(report.persistence),
            'freeBytes': report.free_bytes,
            'latencyMs': report.probe_latency_ms,
            'volatile': _is_volatile(candidate, storage_class),
            'isCurrent': resolved == current_root,
            'limitations': list(report.limitations or [])[:4],
        })

    eligible = [
        entry for entry in entries
        if (entry['writable'] and not entry['volatile'] and not entry['isCurrent']
            and entry['persistence'] == 'persistent'
            and (entry['freeBytes'] or 0) >= _RECOMMEND_MIN_FREE_BYTES
            and entry['latencyMs'] is not None)
    ]
    recommended = (min(eligible, key=lambda entry: entry['latencyMs'])
                   if eligible else None)
    for entry in entries:
        entry['recommended'] = (recommended is not None
                                and entry['path'] == recommended['path'])

    result = {
        'probedAt': time.time(),

        'inProgress': False,
        'candidates': entries,
        'recommendedPath': recommended['path'] if recommended else '',
    }
    with _cache_lock:
        _probe_cache.update({'at': time.monotonic(), 'result': dict(result)})
    return result
