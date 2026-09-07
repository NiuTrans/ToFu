"""Read-only access to a stopped or concurrently WAL-backed SQLite authority.

This module exists only for offline diagnostics. Runtime application code must
use semantic Sidecar operations; diagnostics may open the SQLite file directly
because the live Sidecar token and port are intentionally process-private.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import socket
from typing import Any
from urllib.parse import quote


@dataclass(frozen=True, slots=True)
class SQLiteAuthorityLocation:
    """Credential-free result of read-only authority discovery."""

    path: Path
    source: str
    fastpath_active: bool


class SQLiteAuthorityDiscoveryError(RuntimeError):
    """Raised when no candidate can be proven authoritative."""


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file() or path.stat().st_size > 64 * 1024:
            return None
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _lease_is_live(data_dir: Path, lease: dict[str, Any]) -> bool:
    """Verify the running stamp against the process and held OS lock."""
    if lease.get('status') != 'running':
        return False
    if str(lease.get('host') or '') != socket.gethostname():
        return False
    try:
        pid = int(lease.get('pid') or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False

    lock_path = data_dir / '.storage-sidecar.lock'
    if os.name == 'nt':  # pragma: no cover - Windows CI
        return lock_path.is_file()
    try:
        import fcntl
        with lock_path.open('rb') as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError):
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def _location_from_live_lease(
    data_dir: Path,
    lease: dict[str, Any],
) -> SQLiteAuthorityLocation | None:
    """Resolve the v1 locator a started Sidecar publishes into its lease."""
    locator = lease.get('storage_locator')
    if not isinstance(locator, dict):
        return None
    if locator.get('format') != 'tofu.storage-locator/v1':
        return None
    backend = str(locator.get('backend') or '')
    if backend != 'sqlite':
        raise SQLiteAuthorityDiscoveryError(
            f'active storage backend is {backend or "unknown"}, not SQLite; '
            'use a backend-neutral Sidecar diagnostic')
    raw_path = str(locator.get('authority_path') or '').strip()
    if not raw_path:
        raise SQLiteAuthorityDiscoveryError(
            'active SQLite lease has no authority_path')
    path = Path(raw_path).expanduser().resolve()
    if not path.is_file():
        raise SQLiteAuthorityDiscoveryError(
            f'active SQLite lease names a missing authority: {path}')
    fastpath_active = bool(locator.get('fastpath_active'))
    if fastpath_active:
        from lib.storage_sidecar.fastpath import local_front_matches_shadow
        if not local_front_matches_shadow(path, data_dir):
            raise SQLiteAuthorityDiscoveryError(
                'active lease locator does not match this deployment\'s '
                'fastpath shadow lineage')
    return SQLiteAuthorityLocation(path, 'live_lease_locator', fastpath_active)


def _locations_from_process_fds(
    data_dir: Path,
    lease: dict[str, Any],
) -> list[SQLiteAuthorityLocation]:
    """Compatibility discovery for a live pre-locator Sidecar on Linux."""
    if os.name == 'nt':  # pragma: no cover - Windows CI
        return []
    try:
        pid = int(lease.get('pid') or 0)
    except (TypeError, ValueError):
        return []
    fd_dir = Path('/proc') / str(pid) / 'fd'
    if not fd_dir.is_dir():
        return []

    from lib.storage_sidecar.fastpath import local_front_matches_shadow

    classic = (data_dir / 'tofu.db').resolve()
    found: dict[str, SQLiteAuthorityLocation] = {}
    try:
        descriptors = list(fd_dir.iterdir())[:4096]
    except OSError:
        return []
    for descriptor in descriptors:
        try:
            raw_target = os.readlink(descriptor)
            if raw_target.endswith(' (deleted)'):
                continue
            target = Path(raw_target).resolve()
        except OSError:
            continue
        if target.name != 'tofu.db' or not target.is_file():
            continue
        if target == classic:
            found[str(target)] = SQLiteAuthorityLocation(
                target, 'live_sidecar_open_file', False)
        elif local_front_matches_shadow(target, data_dir):
            found[str(target)] = SQLiteAuthorityLocation(
                target, 'live_sidecar_open_fastpath', True)
    return list(found.values())


def resolve_readonly_sqlite_authority(
    data_dir: str | Path,
    *,
    explicit_path: str | Path | None = None,
    environ: Any = os.environ,
) -> SQLiteAuthorityLocation:
    """Find the one SQLite file a diagnostic may safely call authoritative.

    Resolution is fail-closed. An explicit ``--db`` wins. Otherwise a live
    Sidecar locator is authoritative; a legacy live process is discovered from
    its open files; then manifest-proven fastpath fronts are considered. A
    classic ``data/tofu.db`` is used only when no fastpath shadow exists, so a
    diagnostic can never silently report stale state as current state.
    """
    root = Path(data_dir).expanduser().resolve()
    if explicit_path is not None:
        explicit = Path(explicit_path).expanduser().resolve()
        if not explicit.is_file():
            raise FileNotFoundError(f'SQLite authority does not exist: {explicit}')
        return SQLiteAuthorityLocation(explicit, 'explicit', False)

    lease = _read_json_object(root / '.storage-sidecar-lease.json')
    if lease and _lease_is_live(root, lease):
        located = _location_from_live_lease(root, lease)
        if located is not None:
            return located
        open_locations = _locations_from_process_fds(root, lease)
        if len(open_locations) == 1:
            return open_locations[0]
        if len(open_locations) > 1:
            choices = ', '.join(str(item.path) for item in open_locations)
            raise SQLiteAuthorityDiscoveryError(
                f'live Sidecar has multiple plausible SQLite authorities: {choices}')

    from lib.storage_sidecar.fastpath import (
        SHADOW_DIRNAME,
        matching_local_fronts,
    )
    fronts = matching_local_fronts(root, environ=environ)
    if len(fronts) == 1:
        return SQLiteAuthorityLocation(
            fronts[0].resolve(), 'fastpath_manifest_lineage', True)
    if len(fronts) > 1:
        choices = ', '.join(str(path) for path in fronts)
        raise SQLiteAuthorityDiscoveryError(
            f'multiple fastpath fronts match this deployment: {choices}')

    shadow_manifest = root / SHADOW_DIRNAME / 'manifest.json'
    if shadow_manifest.exists():
        raise SQLiteAuthorityDiscoveryError(
            'a fastpath shadow exists but no unique live write front can be '
            'proven; refusing to inspect possibly stale data/tofu.db. Start '
            'the Sidecar or pass an explicitly verified --db path.')

    classic = root / 'tofu.db'
    if classic.is_file():
        return SQLiteAuthorityLocation(classic.resolve(), 'classic_data_path', False)
    raise FileNotFoundError(f'SQLite authority does not exist under: {root}')


def open_readonly_sqlite_authority(path: str | Path) -> sqlite3.Connection:
    """Open an existing SQLite authority with driver-level write denial."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f'SQLite authority does not exist: {resolved}')
    uri = f'file:{quote(str(resolved))}?mode=ro'
    connection = sqlite3.connect(
        uri,
        uri=True,
        timeout=30,
        isolation_level=None,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA busy_timeout=30000')
    connection.execute('PRAGMA query_only=ON')
    return connection


__all__ = [
    'SQLiteAuthorityDiscoveryError',
    'SQLiteAuthorityLocation',
    'open_readonly_sqlite_authority',
    'resolve_readonly_sqlite_authority',
]
