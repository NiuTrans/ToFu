"""Storage capability discovery and conservative automatic policy selection.

Responsibility: turn one bounded launch-time filesystem probe into a
machine-readable report and a backend-neutral storage plan.  This module does
not move authority bytes, open the production database, or silently weaken a
durability contract.  Callers may automatically apply plans whose durability
is unchanged; a local SQLite write front remains consent-gated because its
acknowledged commits have bounded RPO when the local device is lost.

Entry points: :func:`probe_storage_path`, :func:`describe_mount`, and
:func:`plan_storage`.  Dependencies are Python's standard library only so the
probe works before optional database drivers load.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import time
from typing import Literal
import uuid


CapabilityState = Literal['supported', 'unsupported', 'unknown']
StorageClass = Literal[
    'local-block',
    'network-filesystem',
    'memory-filesystem',
    'container-overlay',
    'userspace-filesystem',
    'unknown',
]
Persistence = Literal['persistent', 'ephemeral', 'unknown']

_NETWORK_FILESYSTEMS = frozenset({
    '9p', 'afs', 'beegfs', 'bgfuse', 'ceph', 'cifs', 'davfs', 'davfs2', 'gfs2',
    'glusterfs', 'gpfs', 'lustre', 'ncpfs', 'nfs', 'nfs4', 'ocfs2',
    'smb', 'smb2', 'smb3', 'sshfs',
})
_LOCAL_BLOCK_FILESYSTEMS = frozenset({
    'apfs', 'bcachefs', 'btrfs', 'exfat', 'ext2', 'ext3', 'ext4', 'f2fs',
    'hfs', 'hfsplus', 'jfs', 'nilfs2', 'ntfs', 'ntfs3', 'reiserfs',
    'ufs', 'vfat', 'xfs', 'zfs',
})
_MEMORY_FILESYSTEMS = frozenset({'ramfs', 'tmpfs'})
_OVERLAY_FILESYSTEMS = frozenset({'aufs', 'overlay', 'overlayfs'})


@dataclass(frozen=True, slots=True)
class MountDescription:
    """Best-effort topology description for the mount containing a path."""

    filesystem_type: str
    mount_point: str
    storage_class: StorageClass
    persistence: Persistence

    def as_dict(self) -> dict[str, str]:
        return {
            'filesystem_type': self.filesystem_type,
            'mount_point': self.mount_point,
            'storage_class': self.storage_class,
            'persistence': self.persistence,
        }


@dataclass(frozen=True, slots=True)
class StorageCapabilityReport:
    """Observed capabilities of one path; false never means "probably"."""

    path: str
    path_exists: bool
    path_created: bool
    filesystem_type: str
    mount_point: str
    storage_class: StorageClass
    persistence: Persistence
    free_bytes: int | None
    writable: bool
    private_files: bool
    file_fsync: bool
    directory_fsync: CapabilityState
    atomic_replace: bool
    exclusive_lock: bool
    sqlite_wal_recovery: bool
    probe_latency_ms: float | None
    limitations: tuple[str, ...]

    @property
    def sqlite_local_authority_ready(self) -> bool:
        return (
            self.storage_class == 'local-block'
            and self.persistence == 'persistent'
            and self.writable
            and self.private_files
            and self.file_fsync
            and self.directory_fsync == 'supported'
            and self.atomic_replace
            and self.exclusive_lock
            and self.sqlite_wal_recovery
        )

    def as_dict(self) -> dict[str, object]:
        return {
            'path': self.path,
            'path_exists': self.path_exists,
            'path_created': self.path_created,
            'filesystem_type': self.filesystem_type,
            'mount_point': self.mount_point,
            'storage_class': self.storage_class,
            'persistence': self.persistence,
            'free_bytes': self.free_bytes,
            'writable': self.writable,
            'private_files': self.private_files,
            'file_fsync': self.file_fsync,
            'directory_fsync': self.directory_fsync,
            'atomic_replace': self.atomic_replace,
            'exclusive_lock': self.exclusive_lock,
            'sqlite_wal_recovery': self.sqlite_wal_recovery,
            'sqlite_local_authority_ready': self.sqlite_local_authority_ready,
            'probe_latency_ms': self.probe_latency_ms,
            'limitations': list(self.limitations),
        }


@dataclass(frozen=True, slots=True)
class AdaptiveStoragePlan:
    """One explainable choice; ``strategy`` is safe to apply right now."""

    strategy: str
    recommended_strategy: str
    decision: str
    reason_code: str
    reason: str
    durability_contract: str
    user_action_required: bool
    evidence: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            'strategy': self.strategy,
            'recommended_strategy': self.recommended_strategy,
            'decision': self.decision,
            'reason_code': self.reason_code,
            'reason': self.reason,
            'durability_contract': self.durability_contract,
            'user_action_required': self.user_action_required,
            'evidence': list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class _WriteProbeResult:
    writable: bool = False
    private_files: bool = False
    file_fsync: bool = False
    directory_fsync: CapabilityState = 'unknown'
    atomic_replace: bool = False
    exclusive_lock: bool = False
    sqlite_wal_recovery: bool = False
    latency_ms: float | None = None
    limitations: tuple[str, ...] = ()


def _decode_mount_path(value: str) -> str:
    for encoded, decoded in (
        ('\\040', ' '), ('\\011', '\t'), ('\\012', '\n'), ('\\134', '\\'),
    ):
        value = value.replace(encoded, decoded)
    return value


def _classify_filesystem(filesystem_type: str) -> tuple[StorageClass, Persistence]:
    normalized = filesystem_type.lower()
    if normalized in _NETWORK_FILESYSTEMS or normalized.startswith(
            ('fuse.beegfs', 'fuse.bgfuse', 'fuse.ceph', 'fuse.glusterfs',
             'fuse.sshfs')):
        return 'network-filesystem', 'unknown'
    if normalized in _MEMORY_FILESYSTEMS:
        return 'memory-filesystem', 'ephemeral'
    if normalized in _OVERLAY_FILESYSTEMS:
        return 'container-overlay', 'unknown'
    if normalized in _LOCAL_BLOCK_FILESYSTEMS:
        return 'local-block', 'persistent'
    if normalized.startswith('fuse'):
        # FUSE describes an implementation mechanism, not a durability or
        # locality guarantee.  Unknown user-space filesystems therefore never
        # become an automatic SQLite front solely because a microbenchmark won.
        return 'userspace-filesystem', 'unknown'
    return 'unknown', 'unknown'


def _read_mountinfo() -> str:
    try:
        return Path('/proc/self/mountinfo').read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        return ''


def describe_mount(
    path: str | Path,
    *,
    mountinfo_text: str | None = None,
    persistence_hint: Persistence | None = None,
) -> MountDescription:
    """Describe the longest Linux mount match, conservatively elsewhere."""
    if persistence_hint not in {None, 'persistent', 'ephemeral', 'unknown'}:
        raise ValueError('persistence_hint must be persistent, ephemeral, or unknown')
    try:
        resolved = str(Path(path).expanduser().resolve(strict=False))
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
    storage_class, persistence = _classify_filesystem(best_filesystem)
    if persistence != 'ephemeral':
        ephemeral_roots = [Path(tempfile.gettempdir()).resolve(strict=False)]
        runtime_root = os.environ.get('XDG_RUNTIME_DIR', '').strip()
        if runtime_root:
            ephemeral_roots.append(Path(runtime_root).resolve(strict=False))
        resolved_path = Path(resolved)
        if any(
            resolved_path == root or resolved_path.is_relative_to(root)
            for root in ephemeral_roots
        ):
            # A persistent block device does not make /tmp or XDG_RUNTIME_DIR
            # a durable lifecycle.  Cleanup/reboot may remove these paths.
            persistence = 'ephemeral'
    if persistence_hint is not None:
        persistence = persistence_hint
    return MountDescription(
        filesystem_type=best_filesystem or 'unknown',
        mount_point=best_mount or '',
        storage_class=storage_class,
        persistence=persistence,
    )


def _limitation(stage: str, exc: BaseException) -> str:
    if isinstance(exc, OSError) and exc.errno:
        name = errno.errorcode.get(exc.errno, f'errno_{exc.errno}').lower()
        return f'{stage}:{name}'
    if isinstance(exc, sqlite3.Error):
        return f'{stage}:sqlite_error'
    return f'{stage}:{type(exc).__name__.lower()}'


def _probe_exclusive_lock(path: Path) -> bool:
    with path.open('r+b') as owner, path.open('r+b') as contender:
        if owner.seek(0, os.SEEK_END) == 0:
            owner.write(b'\0')
            owner.flush()
        owner.seek(0)
        contender.seek(0)
        if os.name == 'nt':  # pragma: no cover - exercised on Windows CI
            import msvcrt
            msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                try:
                    msvcrt.locking(contender.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    return True
                else:
                    msvcrt.locking(contender.fileno(), msvcrt.LK_UNLCK, 1)
                    return False
            finally:
                owner.seek(0)
                msvcrt.locking(owner.fileno(), msvcrt.LK_UNLCK, 1)
        import fcntl
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            try:
                fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError):
                return True
            else:
                fcntl.flock(contender.fileno(), fcntl.LOCK_UN)
                return False
        finally:
            fcntl.flock(owner.fileno(), fcntl.LOCK_UN)


def _probe_sqlite_wal(directory: Path, stem: str) -> bool:
    database = directory / f'{stem}.sqlite3'
    connection: sqlite3.Connection | None = None
    reopened: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(database, isolation_level=None)
        mode = connection.execute('PRAGMA journal_mode=WAL').fetchone()[0]
        if str(mode).lower() != 'wal':
            return False
        connection.execute('PRAGMA synchronous=FULL')
        connection.execute('CREATE TABLE durability_probe(value TEXT NOT NULL)')
        connection.execute('BEGIN IMMEDIATE')
        connection.execute(
            'INSERT INTO durability_probe(value) VALUES (?)', ('committed',))
        connection.commit()
        connection.close()
        connection = None
        reopened = sqlite3.connect(database)
        row = reopened.execute('SELECT value FROM durability_probe').fetchone()
        integrity = reopened.execute('PRAGMA integrity_check').fetchone()[0]
        return row == ('committed',) and integrity == 'ok'
    finally:
        if connection is not None:
            connection.close()
        if reopened is not None:
            reopened.close()
        for suffix in ('', '-wal', '-shm'):
            database.with_name(database.name + suffix).unlink(missing_ok=True)


def _run_write_probe(directory: Path) -> _WriteProbeResult:
    """Exercise primitives independently and remove every probe artifact."""
    stem = f'.tofu-storage-capability-{uuid.uuid4().hex}'
    source = directory / f'{stem}.new'
    target = directory / f'{stem}.ready'
    lock_path = directory / f'{stem}.lock'
    limitations: list[str] = []
    writable = False
    private_files = False
    file_fsync = False
    directory_fsync: CapabilityState = 'unknown'
    atomic_replace = False
    exclusive_lock = False
    sqlite_wal_recovery = False
    latency_ms: float | None = None
    started = time.monotonic()
    payload = os.urandom(4096)
    sqlite_database = directory / f'{stem}.sqlite3'
    try:
        try:
            descriptor = os.open(
                source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb', buffering=0) as stream:
                stream.write(payload)
                writable = True
                os.fsync(stream.fileno())
                file_fsync = True
            private_files = stat.S_IMODE(source.stat().st_mode) & 0o077 == 0
            if not private_files:
                limitations.append('private_files:mode_too_broad')
            latency_ms = (time.monotonic() - started) * 1000.0
        except (OSError, ValueError) as exc:
            limitations.append(_limitation('file_fsync', exc))

        if file_fsync:
            try:
                os.replace(source, target)
                atomic_replace = target.is_file() and target.read_bytes() == payload
                if not atomic_replace:
                    limitations.append('atomic_replace:content_mismatch')
            except OSError as exc:
                limitations.append(_limitation('atomic_replace', exc))

        if hasattr(os, 'O_DIRECTORY'):
            try:
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                directory_fsync = 'supported'
            except OSError as exc:
                directory_fsync = 'unsupported'
                limitations.append(_limitation('directory_fsync', exc))
        else:  # pragma: no cover - platform capability, not a failure guess
            limitations.append('directory_fsync:unknown_platform')

        try:
            descriptor = os.open(
                lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
            exclusive_lock = _probe_exclusive_lock(lock_path)
            if not exclusive_lock:
                limitations.append('exclusive_lock:not_enforced')
        except (OSError, BlockingIOError) as exc:
            limitations.append(_limitation('exclusive_lock', exc))

        if writable:
            try:
                sqlite_wal_recovery = _probe_sqlite_wal(directory, stem)
                if not sqlite_wal_recovery:
                    limitations.append('sqlite_wal_recovery:round_trip_failed')
            except (OSError, sqlite3.Error, RuntimeError) as exc:
                limitations.append(_limitation('sqlite_wal_recovery', exc))
    finally:
        sqlite_sidecars = tuple(
            sqlite_database.with_name(sqlite_database.name + suffix)
            for suffix in ('', '-wal', '-shm'))
        for path in (source, target, lock_path, *sqlite_sidecars):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # Cleanup failure is observable and makes this path unsuitable
                # for automatic selection, even if all durability calls worked.
                limitations.append('cleanup:failed')
                writable = False
    return _WriteProbeResult(
        writable=writable,
        private_files=private_files,
        file_fsync=file_fsync,
        directory_fsync=directory_fsync,
        atomic_replace=atomic_replace,
        exclusive_lock=exclusive_lock,
        sqlite_wal_recovery=sqlite_wal_recovery,
        latency_ms=latency_ms,
        limitations=tuple(dict.fromkeys(limitations)),
    )


def _existing_ancestor(path: Path) -> Path | None:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent
    return candidate


def probe_storage_path(
    path: str | Path,
    *,
    create_directory: bool = False,
    mountinfo_text: str | None = None,
    persistence_hint: Persistence | None = None,
) -> StorageCapabilityReport:
    """Run one bounded, cleanup-safe capability probe.

    A missing directory is created only when ``create_directory`` is true.
    Failures are returned as limitations so an adaptive caller can fall back;
    an explicit required mode may turn that report into a startup error.
    """
    requested = Path(path).expanduser()
    path_created = False
    limitations: list[str] = []
    if not requested.exists() and create_directory:
        try:
            requested.mkdir(mode=0o700, parents=True, exist_ok=True)
            path_created = True
        except OSError as exc:
            limitations.append(_limitation('directory_create', exc))
    path_exists = requested.is_dir()
    if requested.exists() and not path_exists:
        limitations.append('directory:not_a_directory')
    elif not requested.exists():
        limitations.append('directory:missing')

    mount_probe_path = requested if path_exists else _existing_ancestor(requested)
    mount = describe_mount(
        mount_probe_path or requested,
        mountinfo_text=mountinfo_text,
        persistence_hint=persistence_hint,
    )
    free_bytes: int | None = None
    if mount_probe_path is not None:
        try:
            free_bytes = int(shutil.disk_usage(mount_probe_path).free)
        except (OSError, TypeError, ValueError, OverflowError) as exc:
            limitations.append(_limitation('capacity', exc))

    write = _WriteProbeResult(limitations=())
    if path_exists:
        try:
            write = _run_write_probe(requested)
        except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
            limitations.append(_limitation('write_probe', exc))
    limitations.extend(write.limitations)
    if mount.storage_class in {'network-filesystem', 'userspace-filesystem'}:
        limitations.append('sqlite_wal:shared_filesystem_not_auto_safe')
    return StorageCapabilityReport(
        path=str(requested.resolve(strict=False)),
        path_exists=path_exists,
        path_created=path_created,
        filesystem_type=mount.filesystem_type,
        mount_point=mount.mount_point,
        storage_class=mount.storage_class,
        persistence=mount.persistence,
        free_bytes=free_bytes,
        writable=write.writable,
        private_files=write.private_files,
        file_fsync=write.file_fsync,
        directory_fsync=write.directory_fsync,
        atomic_replace=write.atomic_replace,
        exclusive_lock=write.exclusive_lock,
        sqlite_wal_recovery=write.sqlite_wal_recovery,
        probe_latency_ms=(
            round(write.latency_ms, 3) if write.latency_ms is not None else None),
        limitations=tuple(dict.fromkeys(limitations)),
    )


def _front_primitives_ready(report: StorageCapabilityReport) -> bool:
    return (
        report.storage_class in {
            'local-block', 'container-overlay', 'memory-filesystem',
        }
        and report.writable
        and report.private_files
        and report.file_fsync
        and report.directory_fsync == 'supported'
        and report.atomic_replace
        and report.exclusive_lock
        and report.sqlite_wal_recovery
    )


def plan_storage(
    *,
    backend: str,
    authority: StorageCapabilityReport | None = None,
    candidate: StorageCapabilityReport | None = None,
    measured_speedup: float | None = None,
    minimum_speedup: float = 3.0,
    bounded_rpo_consent: bool = False,
) -> AdaptiveStoragePlan:
    """Select a safe current strategy and explain any better recommendation.

    The planner is deliberately pure.  Passing ``bounded_rpo_consent=True``
    records an already-made policy choice; it never prompts, migrates, or
    interprets a writable directory as consent.
    """
    normalized_backend = backend.strip().lower()
    if normalized_backend not in {'sqlite', 'postgres'}:
        raise ValueError('backend must be sqlite or postgres')
    if not math.isfinite(minimum_speedup) or minimum_speedup < 1.0:
        raise ValueError('minimum_speedup must be at least 1.0')
    if measured_speedup is not None and (
            not math.isfinite(measured_speedup) or measured_speedup < 0):
        raise ValueError('measured_speedup must be finite and non-negative')

    if normalized_backend == 'postgres':
        return AdaptiveStoragePlan(
            strategy='client-server',
            recommended_strategy='client-server',
            decision='automatic',
            reason_code='external_authority',
            reason=(
                'The database server owns filesystem durability; this process '
                'uses the client-server adapter.'),
            durability_contract='server-acknowledged-durable-commit',
            user_action_required=False,
        )
    if authority is None:
        raise ValueError('SQLite planning requires an authority capability report')

    direct_evidence = (
        f'authority_class={authority.storage_class}',
        f'authority_wal_recovery={str(authority.sqlite_wal_recovery).lower()}',
    )
    if candidate is None:
        if authority.sqlite_local_authority_ready:
            return AdaptiveStoragePlan(
                strategy='sqlite-direct',
                recommended_strategy='sqlite-direct',
                decision='automatic',
                reason_code='local_authority_ready',
                reason='The current path passed every local SQLite durability probe.',
                durability_contract='authority-fsync-before-ack',
                user_action_required=False,
                evidence=direct_evidence,
            )
        recommendation = (
            'client-server' if authority.storage_class in {
                'network-filesystem', 'userspace-filesystem'}
            else 'sqlite-direct')
        return AdaptiveStoragePlan(
            strategy='sqlite-direct',
            recommended_strategy=recommendation,
            decision='observe',
            reason_code='no_safe_local_candidate',
            reason=(
                'No verified local candidate is available; keep the existing '
                'authority and surface its failed capabilities.'),
            durability_contract='authority-fsync-before-ack',
            user_action_required=False,
            evidence=direct_evidence,
        )

    candidate_evidence = (
        *direct_evidence,
        f'candidate_class={candidate.storage_class}',
        f'candidate_persistence={candidate.persistence}',
        f'measured_speedup={measured_speedup}',
    )
    if not _front_primitives_ready(candidate):
        return AdaptiveStoragePlan(
            strategy='sqlite-direct',
            recommended_strategy=(
                'client-server' if authority.storage_class in {
                    'network-filesystem', 'userspace-filesystem'}
                else 'sqlite-direct'),
            decision='blocked',
            reason_code='candidate_capabilities_failed',
            reason=(
                'The candidate did not prove local fsync, atomic replace, '
                'exclusive locking, private files, and SQLite WAL recovery.'),
            durability_contract='authority-fsync-before-ack',
            user_action_required=False,
            evidence=candidate_evidence,
        )
    if measured_speedup is None:
        return AdaptiveStoragePlan(
            strategy='sqlite-direct',
            recommended_strategy='sqlite-direct',
            decision='observe',
            reason_code='benchmark_required',
            reason='The candidate is capable, but no measured commit-latency win exists.',
            durability_contract='authority-fsync-before-ack',
            user_action_required=False,
            evidence=candidate_evidence,
        )
    if measured_speedup < minimum_speedup:
        return AdaptiveStoragePlan(
            strategy='sqlite-direct',
            recommended_strategy='sqlite-direct',
            decision='automatic',
            reason_code='insufficient_measured_win',
            reason='The candidate is safe but not fast enough to justify relocation.',
            durability_contract='authority-fsync-before-ack',
            user_action_required=False,
            evidence=candidate_evidence,
        )
    if not bounded_rpo_consent:
        return AdaptiveStoragePlan(
            strategy='sqlite-direct',
            recommended_strategy='sqlite-local-front',
            decision='consent-required',
            reason_code='durability_change_requires_consent',
            reason=(
                'The local front is verified and faster, but device loss can '
                'forfeit its unshipped tail; a writable path is not consent.'),
            durability_contract='authority-fsync-before-ack',
            user_action_required=True,
            evidence=candidate_evidence,
        )
    return AdaptiveStoragePlan(
        strategy='sqlite-local-front',
        recommended_strategy='sqlite-local-front',
        decision='automatic-after-consent',
        reason_code='verified_local_front',
        reason='The candidate passed every probe and exceeded the measured speedup gate.',
        durability_contract='bounded-rpo-local-ack-with-durable-shadow',
        user_action_required=False,
        evidence=candidate_evidence,
    )


def report_digest(report: StorageCapabilityReport) -> str:
    """Stable identifier for logs/metrics without duplicating a large report."""
    encoded = json.dumps(
        report.as_dict(), sort_keys=True, separators=(',', ':'),
        ensure_ascii=True,
    ).encode('ascii')
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    'AdaptiveStoragePlan',
    'CapabilityState',
    'MountDescription',
    'Persistence',
    'StorageClass',
    'StorageCapabilityReport',
    'describe_mount',
    'plan_storage',
    'probe_storage_path',
    'report_digest',
]
