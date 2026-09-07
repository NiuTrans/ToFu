"""Bounded lifecycle policy for personal SQLite recovery artifacts.

The Sidecar owns backup publication.  This module contains only filesystem
policy shared by the online backend and the offline maintenance command:
capacity admission, crash-artifact reclamation, and bounded retention/rotation.
It never opens the authority database.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat as stat_mode
import time
from typing import Any

from lib.storage.errors import StorageError
from lib.storage_sidecar.durability import fsync_directory, write_json_durable
from runtime_guards import resolve_resource_budget


_ARTIFACT_PREFIX = 'storage-sqlite-'
_ARTIFACT_SUFFIX = '.sqlite3'
_JOB_SUFFIX = '.job.json'
_MIN_RESERVE_BYTES = 64 * 1024 * 1024
_DEFAULT_RETENTION = 2
_DEFAULT_TEMP_TTL_S = 60 * 60
_DEFAULT_ROLLBACK_RETENTION = 1
_MAX_ROLLBACK_RETENTION = 4
_MIB = 1024 * 1024
_MAX_RECOVERY_COPY_BUDGET_MIB = 8 * 1024 * 1024
_ROLLBACK_ARTIFACT_RE = re.compile(
    r'^tofu\.db\.pre-compact-\d{8}T\d{6}Z$')
_LEGACY_SNAPSHOT_TEMP_RE = re.compile(
    r'^\.tofu-\d{8}_\d{6}-(?P<pid>[1-9]\d*)-[0-9a-f]{8}'
    r'\.sqlite3\.tmp-[0-9a-f]{32}$')
_LEGACY_SNAPSHOT_SCAN_LIMIT = 256
_SQLITE_TEMP_COMPANION_SUFFIXES = ('-journal', '-wal', '-shm')


def _positive_environment_integer(name: str, default: int) -> int:
    raw = os.environ.get(name, '').strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _nonnegative_environment_integer(name: str, default: int) -> int:
    raw = os.environ.get(name, '').strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def retention_count() -> int:
    """Keep the newest verified backup plus one prior recovery point."""
    return _positive_environment_integer(
        'TOFU_STORAGE_SQLITE_BACKUP_RETENTION', _DEFAULT_RETENTION)


def rollback_retention_count() -> int:
    """Bound verified-copy deep-clean rollback points by count.

    At least one rollback is always retained automatically. Removing the final
    recovery point is an explicit operator action owned by the deep-clean CLI.
    """
    configured = _positive_environment_integer(
        'TOFU_STORAGE_SQLITE_ROLLBACK_RETENTION',
        _DEFAULT_ROLLBACK_RETENTION,
    )
    return min(configured, _MAX_ROLLBACK_RETENTION)


def recovery_copy_budget_bytes() -> int:
    """Return the boot-probed recovery-copy ceiling with a hard override cap."""
    return resolve_resource_budget(
        'TOFU_STORAGE_RECOVERY_COPY_BUDGET_MIB',
        os.environ,
        minimum=4096,
        maximum=_MAX_RECOVERY_COPY_BUDGET_MIB,
    ) * _MIB


def resolve_rollback_artifact(data_dir: Path, basename: str) -> Path:
    """Resolve one exact tool-owned rollback basename without path traversal."""
    name = str(basename or '')
    if Path(name).name != name or not _ROLLBACK_ARTIFACT_RE.fullmatch(name):
        raise ValueError('invalid SQLite deep-clean rollback basename')
    artifact = data_dir / name
    if artifact.is_symlink() or not artifact.is_file():
        raise ValueError('SQLite deep-clean rollback is missing or unsafe')
    return artifact


def retained_rollback_artifacts(data_dir: Path) -> list[Path]:
    """Return safe regular rollback artifacts, newest first."""
    candidates: list[tuple[int, str, Path]] = []
    try:
        entries = list(data_dir.iterdir())
    except FileNotFoundError:
        return []
    for path in entries:
        if not _ROLLBACK_ARTIFACT_RE.fullmatch(path.name):
            continue
        try:
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            continue
        candidates.append((int(stat.st_mtime_ns), path.name, path))
    return [
        path for _mtime_ns, _name, path
        in sorted(candidates, reverse=True)
    ]


def rollback_artifact_inventory(data_dir: Path) -> dict[str, Any]:
    """Report logical/allocated rollback weight without opening the files."""
    now = time.time()
    artifacts: list[dict[str, Any]] = []
    for path in retained_rollback_artifacts(data_dir):
        try:
            stat = path.stat()
        except OSError:
            continue
        allocated = int(getattr(stat, 'st_blocks', 0) or 0) * 512
        if allocated <= 0:
            allocated = int(stat.st_size)
        artifacts.append({
            'name': path.name,
            'path': str(path),
            'logical_bytes': int(stat.st_size),
            'allocated_bytes': allocated,
            'modified_at_unix_s': round(float(stat.st_mtime), 3),
            'age_s': max(0, int(now - stat.st_mtime)),
        })
    keep = rollback_retention_count()
    return {
        'retention_count': keep,
        'count': len(artifacts),
        'excess_count': max(0, len(artifacts) - keep),
        'total_logical_bytes': sum(row['logical_bytes'] for row in artifacts),
        'total_allocated_bytes': sum(
            row['allocated_bytes'] for row in artifacts),
        'artifacts': artifacts,
    }


def verified_backup_inventory(backups: Path) -> dict[str, Any]:
    """Report published SQLite backup weight without reading authority pages."""
    artifacts: list[dict[str, Any]] = []
    try:
        entries = list(backups.iterdir())
    except FileNotFoundError:
        entries = []
    for path in entries:
        if (not path.name.startswith(_ARTIFACT_PREFIX)
                or not path.name.endswith(_ARTIFACT_SUFFIX)):
            continue
        try:
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            continue
        allocated = int(getattr(stat, 'st_blocks', 0) or 0) * 512
        if allocated <= 0:
            allocated = int(stat.st_size)
        manifest = path.with_name(path.name + '.manifest.json')
        artifacts.append({
            'name': path.name,
            'path': str(path),
            'logical_bytes': int(stat.st_size),
            'allocated_bytes': allocated,
            'modified_at_unix_s': round(float(stat.st_mtime), 3),
            'manifest_present': manifest.is_file() and not manifest.is_symlink(),
        })
    artifacts.sort(
        key=lambda row: (row['modified_at_unix_s'], row['name']),
        reverse=True,
    )
    keep = retention_count()
    return {
        'retention_count': keep,
        'count': len(artifacts),
        'excess_count': max(0, len(artifacts) - keep),
        'total_logical_bytes': sum(row['logical_bytes'] for row in artifacts),
        'total_allocated_bytes': sum(
            row['allocated_bytes'] for row in artifacts),
        'artifacts': artifacts,
    }


def recovery_copy_footprint(backups: Path) -> dict[str, int | bool]:
    """Measure retained copies that share the prospective backup volume.

    A Compose/NAS backup mount has its own device and therefore its own copy
    budget. When backups live on the data volume, retained deep-clean rollback
    points consume the same finite disk and must participate in admission.
    """
    verified_bytes = int(
        verified_backup_inventory(backups)['total_allocated_bytes'])
    data_dir = backups.parent
    try:
        rollback_same_volume = (
            backups.stat().st_dev == data_dir.stat().st_dev)
    except OSError:
        # Capacity admission fails conservatively when mount identity cannot be
        # established; ordinary authority writes remain completely unaffected.
        rollback_same_volume = True
    rollback_bytes = (
        int(rollback_artifact_inventory(data_dir)['total_allocated_bytes'])
        if rollback_same_volume else 0
    )
    return {
        'verified_backup_bytes': verified_bytes,
        'same_volume_rollback_bytes': rollback_bytes,
        'retained_recovery_bytes': verified_bytes + rollback_bytes,
        'rollback_same_volume': rollback_same_volume,
    }


def prune_retained_rollbacks(data_dir: Path, *, preserve: Path) -> dict[str, Any]:
    """Keep the newest bounded rollback set after publishing ``preserve``.

    The just-created recovery point must already exist as a safe regular file;
    otherwise nothing is removed. Sidecar companions are treated as an
    abnormal/incomplete artifact and retained for operator inspection.
    """
    candidates = retained_rollback_artifacts(data_dir)
    if preserve not in candidates:
        return {
            'retention_count': rollback_retention_count(),
            'removed': [],
            'errors': ['new rollback recovery point is missing or unsafe'],
        }
    retention = rollback_retention_count()
    keep = {preserve}
    keep.update([
        artifact
        for artifact in candidates
        if artifact != preserve
    ][:max(0, retention - 1)])
    # The just-published recovery point counts toward the hard retention cap.
    # Filtering before the slice means a surprising old mtime cannot retain
    # N+1 files.
    removed: list[str] = []
    errors: list[str] = []
    for artifact in candidates:
        if artifact in keep:
            continue
        companions = [Path(f'{artifact}-wal'), Path(f'{artifact}-shm')]
        if any(path.exists() for path in companions):
            errors.append(f'{artifact.name}: unexpected WAL/SHM companion')
            continue
        try:
            artifact.unlink()
            removed.append(artifact.name)
        except OSError as exc:
            errors.append(f'{artifact.name}: {type(exc).__name__}: {exc}')
    if removed:
        try:
            fsync_directory(data_dir)
        except OSError as exc:
            errors.append(f'directory fsync: {type(exc).__name__}: {exc}')
    return {
        'retention_count': retention,
        'removed': removed,
        'errors': errors,
    }


def capacity_preflight(
    backups: Path,
    estimated_bytes: int,
    *,
    allow_verified_rotation: bool = False,
) -> dict[str, Any]:
    """Reject a copy unless both free-space and product-copy budgets fit.

    ``allow_verified_rotation`` plans an atomic replacement of older verified
    backups when the steady-state recovery set fits but the pre-publication
    peak does not.  It never mutates the directory.  The caller must guarantee
    zero-copy publication, verify and atomically publish the replacement first,
    then pass ``retire_verified_artifacts`` to
    :func:`prune_verified_backups`.  Classic/cross-device copies retain the
    stricter peak-footprint admission.
    """
    estimate = max(0, int(estimated_bytes))
    reserve = max(_MIN_RESERVE_BYTES, int(estimate * 0.05))
    reserve = _nonnegative_environment_integer(
        'TOFU_STORAGE_SQLITE_BACKUP_RESERVE_BYTES', reserve)
    footprint = recovery_copy_footprint(backups)
    copy_budget = recovery_copy_budget_bytes()
    retained = int(footprint['retained_recovery_bytes'])
    peak_projected = retained + estimate
    projected = peak_projected
    retire_verified_artifacts: list[str] = []
    if peak_projected > copy_budget and allow_verified_rotation:
        # Rollback points are never eligible for automatic backup rotation.
        # Keep the newest old verified points that fit alongside the incoming
        # point, up to the configured total-retention target minus that new
        # point. Everything else may be retired only after publication.
        rollback_bytes = int(footprint['same_volume_rollback_bytes'])
        old_backup_budget = copy_budget - rollback_bytes - estimate
        if old_backup_budget >= 0:
            inventory = verified_backup_inventory(backups)
            old_keep_limit = max(0, retention_count() - 1)
            kept_old_bytes = 0
            kept_old_count = 0
            for artifact in inventory['artifacts']:
                allocated_bytes = int(artifact['allocated_bytes'])
                if (kept_old_count < old_keep_limit
                        and kept_old_bytes + allocated_bytes
                        <= old_backup_budget):
                    kept_old_bytes += allocated_bytes
                    kept_old_count += 1
                else:
                    retire_verified_artifacts.append(str(artifact['name']))
            projected = rollback_bytes + estimate + kept_old_bytes
    rotation_required = bool(
        peak_projected > copy_budget
        and retire_verified_artifacts
        and projected <= copy_budget
    )
    if peak_projected > copy_budget and not rotation_required:
        raise StorageError(
            'database_unavailable',
            'SQLite recovery-copy budget exceeded '
            f'({peak_projected} > {copy_budget} bytes); use an independent backup '
            'volume or raise the explicit bounded budget',
            retryable=False,
        )
    free = int(shutil.disk_usage(backups).free)
    required = estimate + reserve
    result: dict[str, Any] = {
        'ok': free >= required,
        'free_bytes': free,
        'estimated_bytes': estimate,
        'reserve_bytes': reserve,
        'required_bytes': required,
        'recovery_copy_budget_bytes': copy_budget,
        'retained_recovery_bytes': retained,
        'projected_recovery_bytes': projected,
        'peak_projected_recovery_bytes': peak_projected,
        'same_volume_rollback_bytes': int(
            footprint['same_volume_rollback_bytes']),
        'budget_rotation_required': rotation_required,
        'retire_verified_artifacts': retire_verified_artifacts,
    }
    if not result['ok']:
        raise StorageError(
            'database_unavailable',
            'Insufficient capacity for a verified SQLite backup',
            retryable=False,
        )
    return result


def _pid_is_alive(value: Any) -> bool:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return False
    if pid < 1:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def job_manifest_path(temporary: Path) -> Path:
    return Path(f'{temporary}{_JOB_SUFFIX}')


def write_job_manifest(
    temporary: Path,
    *,
    source: Path,
    state: str,
    extra: dict[str, Any] | None = None,
) -> Path:
    manifest = job_manifest_path(temporary)
    payload: dict[str, Any] = {
        'version': 1,
        'pid': os.getpid(),
        'source': str(source),
        'temporary_path': str(temporary),
        'state': state,
        'updated_at': time.time(),
    }
    if extra:
        payload.update(extra)
    write_json_durable(manifest, payload)
    return manifest


def cleanup_job_artifacts(temporary: Path) -> None:
    temporary.unlink(missing_ok=True)
    job_manifest_path(temporary).unlink(missing_ok=True)


def _reclaim_stale_legacy_snapshot_artifacts(
    backups: Path,
    *,
    ttl_s: int,
    now: float,
) -> int:
    """Reclaim only proven-dead unpublished artifacts of the retired owner.

    The pre-Sidecar backup producer wrote a strict timestamp/PID/UUID name in
    ``data/db_snapshots`` and promised that its next job would remove an
    interrupted temporary copy. Migrating the producer retired that next-job
    cleanup, leaving database-sized partial copies permanently allocated.

    Published ``tofu-*.sqlite3`` recovery points never match. A temporary must
    be a regular non-symlink file, older than the current temporary TTL, and
    name a dead PID. A fresh/live valid manifest protects it too; malformed or
    unsafe manifests and companions fail closed. The shallow scan is bounded
    independently of directory size.
    """
    snapshot_dir = backups.parent / 'db_snapshots'
    try:
        entries = os.scandir(snapshot_dir)
    except OSError:
        return 0

    removed = 0
    scanned = 0
    with entries:
        for entry in entries:
            scanned += 1
            if scanned > _LEGACY_SNAPSHOT_SCAN_LIMIT:
                break
            match = _LEGACY_SNAPSHOT_TEMP_RE.fullmatch(entry.name)
            if match is None:
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                initial = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if (
                now - float(initial.st_mtime) <= ttl_s
                or _pid_is_alive(match.group('pid'))
            ):
                continue

            temporary = snapshot_dir / entry.name
            manifest = job_manifest_path(temporary)
            owned_artifacts = [temporary]
            unsafe = False
            if manifest.exists() or manifest.is_symlink():
                try:
                    manifest_stat = manifest.lstat()
                    if (
                        manifest.is_symlink()
                        or not stat_mode.S_ISREG(manifest_stat.st_mode)
                    ):
                        unsafe = True
                    else:
                        value = json.loads(manifest.read_text(encoding='utf-8'))
                        payload = value if isinstance(value, dict) else {}
                        if (
                            now - float(manifest_stat.st_mtime) <= ttl_s
                            or _pid_is_alive(payload.get('pid'))
                        ):
                            continue
                        owned_artifacts.append(manifest)
                except (OSError, UnicodeError, json.JSONDecodeError):
                    unsafe = True
            for suffix in _SQLITE_TEMP_COMPANION_SUFFIXES:
                companion = Path(f'{temporary}{suffix}')
                if not (companion.exists() or companion.is_symlink()):
                    continue
                try:
                    companion_stat = companion.lstat()
                except OSError:
                    unsafe = True
                    continue
                if (
                    companion.is_symlink()
                    or not stat_mode.S_ISREG(companion_stat.st_mode)
                ):
                    unsafe = True
                    continue
                owned_artifacts.append(companion)
            if unsafe:
                continue

            # Revalidate the large primary after inspecting its sidecars. A
            # replacement or active write races toward preservation, never an
            # unlink of a file other than the one proved above.
            try:
                current = temporary.lstat()
            except OSError:
                continue
            initial_identity = (
                int(initial.st_dev), int(initial.st_ino), int(initial.st_size),
                int(initial.st_mtime_ns),
            )
            current_identity = (
                int(current.st_dev), int(current.st_ino), int(current.st_size),
                int(current.st_mtime_ns),
            )
            if (
                initial_identity != current_identity
                or not stat_mode.S_ISREG(current.st_mode)
            ):
                continue

            # Companions/manifests go first. If process loss interrupts this
            # cleanup, the still-present primary is safely rediscovered next
            # time; deleting the primary is the final publication boundary.
            for artifact in [*owned_artifacts[1:], temporary]:
                try:
                    artifact.unlink()
                    removed += 1
                except FileNotFoundError:
                    pass
    if removed:
        fsync_directory(snapshot_dir)
    return removed


def reclaim_stale_job_artifacts(backups: Path) -> int:
    """Remove expired current and retired temporary copies with dead owners."""
    ttl_s = _positive_environment_integer(
        'TOFU_STORAGE_SQLITE_BACKUP_TEMP_TTL_SECONDS', _DEFAULT_TEMP_TTL_S)
    now = time.time()
    removed = 0
    protected: set[Path] = set()
    for manifest in backups.glob(f'.{_ARTIFACT_PREFIX}*.tmp-*{_JOB_SUFFIX}'):
        temporary = Path(str(manifest)[:-len(_JOB_SUFFIX)])
        try:
            age_s = now - manifest.stat().st_mtime
            value = json.loads(manifest.read_text(encoding='utf-8'))
            payload = value if isinstance(value, dict) else {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        if age_s <= ttl_s or _pid_is_alive(payload.get('pid')):
            protected.add(temporary)
            continue
        for artifact in (temporary, manifest):
            try:
                artifact.unlink()
                removed += 1
            except FileNotFoundError:
                pass

    for temporary in backups.glob(f'.{_ARTIFACT_PREFIX}*.tmp-*'):
        if temporary.name.endswith(_JOB_SUFFIX) or temporary in protected:
            continue
        if job_manifest_path(temporary).exists():
            continue
        try:
            if now - temporary.stat().st_mtime <= ttl_s:
                continue
            temporary.unlink()
            removed += 1
        except FileNotFoundError:
            pass
    removed += _reclaim_stale_legacy_snapshot_artifacts(
        backups,
        ttl_s=ttl_s,
        now=now,
    )
    return removed


def prune_verified_backups(
    backups: Path,
    *,
    preserve: Path,
    retire_names: set[str] | None = None,
) -> int:
    """Bound full copies by count/budget after ``preserve`` is published.

    ``retire_names`` must come from a non-mutating capacity plan.  The newly
    verified ``preserve`` point always wins, so a fault before publication
    cannot remove the previous recovery point.
    """
    candidates = sorted(
        (
            path for path in backups.glob(f'{_ARTIFACT_PREFIX}*{_ARTIFACT_SUFFIX}')
            if path.is_file()
        ),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    keep = set(candidates[:retention_count()])
    keep.add(preserve)
    forced_retire = set(retire_names or ())
    removed = 0
    for artifact in candidates:
        if artifact == preserve:
            continue
        if artifact in keep and artifact.name not in forced_retire:
            continue
        artifact.unlink(missing_ok=True)
        artifact.with_name(artifact.name + '.manifest.json').unlink(missing_ok=True)
        removed += 1
    return removed


__all__ = [
    'capacity_preflight',
    'cleanup_job_artifacts',
    'job_manifest_path',
    'prune_verified_backups',
    'reclaim_stale_job_artifacts',
    'prune_retained_rollbacks',
    'resolve_rollback_artifact',
    'retained_rollback_artifacts',
    'rollback_artifact_inventory',
    'rollback_retention_count',
    'retention_count',
    'verified_backup_inventory',
    'write_job_manifest',
]
