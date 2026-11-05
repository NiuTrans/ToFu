"""Choose fast local storage for runtime/build/index caches and task checkouts.

Responsibility: classify the volume holding a path, measure its metadata
latency, and select one private local root for a declared purpose only when
that root is decisively faster than the source volume. The lifecycle manager
(reconstructible worker runtime replicas) and the task workspace control
plane (checkouts holding work in progress) share this one placement policy;
only the persistence they require differs. Native compiler snapshots reuse
the runtime cache mode and disk policy under their own native-builds leaf.

Entry points: :func:`describe_volume`, :func:`measure_lookup_latency`,
:func:`candidate_roots`, :func:`choose_local_root`, :func:`has_free_space`,
:func:`index_cache_placement`,
and ``python -m serverctl_pkg.local_placement [SOURCE]``, which prints the
decisions for diagnostics.

Dependencies: Python standard library only. The manager imports this before
application dependencies are known to be healthy; ``lib`` callers import it
without loading the application package.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import time

PURPOSE_RUNTIME = 'runtime-replica'
PURPOSE_NATIVE_BUILD = 'native-build'
PURPOSE_WORKSPACES = 'task-workspaces'
PURPOSE_INDEXES = 'reconstructible-indexes'
PURPOSE_NATIVE_AUTHORITY = 'native-authority'
MODE_KNOBS = {
    PURPOSE_RUNTIME: 'TOFU_RUNTIME_REPLICA',
    PURPOSE_NATIVE_BUILD: 'TOFU_RUNTIME_REPLICA',
    PURPOSE_WORKSPACES: 'TOFU_LOCAL_WORKSPACES',
    PURPOSE_INDEXES: 'TOFU_INDEX_FASTPATH',
    PURPOSE_NATIVE_AUTHORITY: 'TOFU_DB_FASTPATH',
}
# Runtime replicas are reconstructible caches and place themselves. Task
# checkouts hold uncommitted work, so moving them off the data volume is an
# operator decision. The native authority front holds acknowledged commits
# behind an asynchronous shadow: ``auto`` relocates it only onto proven
# persistent local volumes, and the native admission still decides.
DEFAULT_MODES = {
    PURPOSE_RUNTIME: 'auto',
    PURPOSE_NATIVE_BUILD: 'auto',
    PURPOSE_WORKSPACES: 'off',
    PURPOSE_INDEXES: 'auto',
    PURPOSE_NATIVE_AUTHORITY: 'auto',
}
_ROOT_LEAVES = {
    PURPOSE_RUNTIME: 'runtime-replicas',
    PURPOSE_NATIVE_BUILD: 'native-builds',
    PURPOSE_WORKSPACES: 'workspaces',
    PURPOSE_INDEXES: 'indexes',
    PURPOSE_NATIVE_AUTHORITY: 'native-authority',
}
# Purposes whose placed tree holds durable or in-progress state, not a cache.
_DURABLE_PURPOSES = frozenset({PURPOSE_WORKSPACES, PURPOSE_NATIVE_AUTHORITY})

MIN_SPEEDUP = 3.0
# Local disks answer an uncached lookup in single-digit microseconds; network
# and userspace filesystems need a round trip. Consulted only when the mount
# table cannot classify the source (for example on macOS).
SLOW_LOOKUP_US = 50.0
_GIB = 1024 ** 3
_RESERVE_FLOOR_BYTES = 2 * _GIB
_RESERVE_CEILING_BYTES = 8 * _GIB
_RESERVE_FRACTION = 0.05
_LATENCY_SAMPLES = 24

_OFF_VALUES = frozenset({'0', 'false', 'no', 'off', 'disabled'})
_ON_VALUES = frozenset({'1', 'true', 'yes', 'on', 'enabled', 'force'})

_NETWORK_FILESYSTEMS = frozenset({
    '9p', 'afs', 'beegfs', 'bgfuse', 'ceph', 'cifs', 'davfs', 'davfs2',
    'gfs2', 'glusterfs', 'gpfs', 'lustre', 'ncpfs', 'nfs', 'nfs4', 'ocfs2',
    'smb', 'smb2', 'smb3', 'sshfs',
})
_NETWORK_FUSE_PREFIXES = (
    'fuse.beegfs', 'fuse.bgfuse', 'fuse.ceph', 'fuse.glusterfs', 'fuse.sshfs',
)
_LOCAL_BLOCK_FILESYSTEMS = frozenset({
    'apfs', 'bcachefs', 'btrfs', 'exfat', 'ext2', 'ext3', 'ext4', 'f2fs',
    'hfs', 'hfsplus', 'jfs', 'nilfs2', 'ntfs', 'ntfs3', 'reiserfs', 'ufs',
    'vfat', 'xfs', 'zfs',
})
_MEMORY_FILESYSTEMS = frozenset({'ramfs', 'tmpfs'})
_OVERLAY_FILESYSTEMS = frozenset({'aufs', 'overlay', 'overlayfs'})

REMOTE_CLASSES = frozenset({'network-filesystem', 'userspace-filesystem'})
LOCAL_CLASSES = frozenset({'local-block', 'container-overlay'})


@dataclass(frozen=True, slots=True)
class Volume:
    """Topology of the mount containing one path.

    Classification matches ``lib.storage_authority_sql.storage_capabilities.
    describe_mount``, the storage authority's probe, which the manager cannot
    import before application dependencies are known to load; a parity test
    keeps the two identical. ``executable`` is false on ``noexec`` mounts.
    """

    filesystem_type: str = 'unknown'
    mount_point: str = ''
    storage_class: str = 'unknown'
    persistence: str = 'unknown'
    executable: bool = True
    device: str = ''


@dataclass(frozen=True, slots=True)
class Placement:
    """One explainable placement decision; ``root`` is empty unless selected."""

    selected: bool
    purpose: str
    mode: str
    reason: str
    root: str = ''
    source: Volume = Volume()
    target: Volume = Volume()
    source_latency_us: float | None = None
    target_latency_us: float | None = None
    free_bytes: int | None = None

    def as_status(self) -> dict[str, object]:
        speedup = (self.source_latency_us / self.target_latency_us
                   if self.source_latency_us and self.target_latency_us else None)
        return {
            'selected': self.selected,
            'purpose': self.purpose,
            'mode': self.mode,
            'reason': self.reason,
            'root': self.root,
            'sourceFilesystem': self.source.filesystem_type,
            'sourceClass': self.source.storage_class,
            'targetFilesystem': self.target.filesystem_type,
            'targetClass': self.target.storage_class,
            'targetPersistence': self.target.persistence,
            'sourceLatencyUs': _rounded(self.source_latency_us),
            'targetLatencyUs': _rounded(self.target_latency_us),
            'speedup': round(speedup, 1) if speedup else None,
            'freeBytes': self.free_bytes,
        }


def _rounded(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


def source_timestamp_is_stable(mtime_ns: int, ctime_ns: int, *, now_ns: int | None = None) -> bool:
    """Do not cache within a coarse filesystem's same-timestamp write window.

    BeeGFS/FUSE can expose whole-second ctime/mtime even through stat_ns;
    other filesystems may use a coarse kernel clock with fractional values.
    A same-size write during that second is invisible to a metadata identity.
    Only copy after that window has closed; subsequent writes then change the
    identity. This is cache eligibility, never admission of a user operation.
    """
    now = time.time_ns() if now_ns is None else now_ns
    return now - max(mtime_ns, ctime_ns) >= 2_000_000_000


def _decode_mount_path(value: str) -> str:
    for encoded, decoded in (
        ('\\040', ' '), ('\\011', '\t'), ('\\012', '\n'), ('\\134', '\\'),
    ):
        value = value.replace(encoded, decoded)
    return value


def classify_filesystem(filesystem_type: str) -> str:
    """Map a kernel filesystem type to a storage class."""
    normalized = filesystem_type.lower()
    if (normalized in _NETWORK_FILESYSTEMS
            or normalized.startswith(_NETWORK_FUSE_PREFIXES)):
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


def read_mountinfo() -> str:
    try:
        return Path('/proc/self/mountinfo').read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        return ''


def _resolved(path: str | os.PathLike[str]) -> str:
    try:
        return str(Path(path).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        return os.path.abspath(os.fspath(path))


def _is_within(path: str, root: str) -> bool:
    root = root.rstrip(os.sep) or os.sep
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def ephemeral_roots(environ: Mapping[str, str]) -> list[str]:
    """Directories whose files cleaners may delete one at a time."""
    roots = [_resolved(tempfile.gettempdir())]
    runtime_dir = str(environ.get('XDG_RUNTIME_DIR', '') or '').strip()
    if runtime_dir:
        roots.append(_resolved(runtime_dir))
    return roots


def describe_volume(
    path: str | os.PathLike[str],
    *,
    mountinfo_text: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Volume:
    """Describe the longest matching Linux mount; unknown elsewhere."""
    resolved = _resolved(path)
    text = read_mountinfo() if mountinfo_text is None else mountinfo_text
    best_mount = ''
    best_filesystem = ''
    best_options: list[str] = []
    best_device = ''
    for line in text.splitlines():
        before, separator, after = line.partition(' - ')
        if not separator:
            continue
        mount_fields = before.split()
        filesystem_fields = after.split()
        if len(mount_fields) < 5 or not filesystem_fields:
            continue
        mount_point = _decode_mount_path(mount_fields[4]).rstrip(os.sep) or os.sep
        if _is_within(resolved, mount_point) and len(mount_point) >= len(best_mount):
            best_mount = mount_point
            best_filesystem = filesystem_fields[0]
            best_options = mount_fields[5].split(',') if len(mount_fields) > 5 else []
            best_device = mount_fields[2]
    storage_class = classify_filesystem(best_filesystem)
    persistence = {
        'local-block': 'persistent',
        'memory-filesystem': 'ephemeral',
    }.get(storage_class, 'unknown')
    environment = os.environ if environ is None else environ
    if persistence != 'ephemeral' and any(
            _is_within(resolved, root) for root in ephemeral_roots(environment)):
        # A persistent device does not make /tmp or XDG_RUNTIME_DIR a durable
        # lifecycle: age-based cleaners remove individual old files.
        persistence = 'ephemeral'
    return Volume(
        filesystem_type=best_filesystem or 'unknown',
        mount_point=best_mount,
        storage_class=storage_class,
        persistence=persistence,
        executable='noexec' not in best_options,
        device=best_device,
    )


def measure_lookup_latency(
    directory: str | os.PathLike[str],
    *,
    samples: int = _LATENCY_SAMPLES,
) -> float | None:
    """Median microseconds of uncached metadata lookups inside ``directory``.

    Fresh random names miss every client cache, so this observes the round
    trip a slow volume charges for each import probe, stat or failed open.
    """
    try:
        if not os.path.isdir(directory):
            return None
        timings = []
        for _ in range(max(3, samples)):
            probe = os.path.join(directory, f'.tofu-latency-{os.urandom(16).hex()}')
            started = time.perf_counter()
            try:
                os.stat(probe)
            except FileNotFoundError:
                pass
            else:
                return None
            timings.append(time.perf_counter() - started)
        return sorted(timings)[len(timings) // 2] * 1e6
    except OSError:
        return None


def placement_mode(purpose: str, environ: Mapping[str, str] | None = None) -> str:
    """``off``, ``auto`` or ``on``; unset or unrecognized means the default."""
    environment = os.environ if environ is None else environ
    value = str(environment.get(MODE_KNOBS[purpose], '') or '').strip().lower()
    if value in _OFF_VALUES:
        return 'off'
    if value in _ON_VALUES:
        return 'on'
    if value == 'auto':
        return 'auto'
    return DEFAULT_MODES[purpose]


def _home(environ: Mapping[str, str]) -> Path | None:
    home = str(environ.get('HOME', '') or '').strip()
    if not home:
        try:
            home = str(Path.home())
        except (KeyError, RuntimeError):
            return None
    if not os.path.isabs(home) or _resolved(home) == os.sep:
        return None
    return Path(home)


def _xdg(environ: Mapping[str, str], name: str, fallback: tuple[str, ...]) -> Path | None:
    configured = str(environ.get(name, '') or '').strip()
    if configured and os.path.isabs(configured):
        return Path(configured)
    home = _home(environ)
    return home.joinpath(*fallback) if home is not None else None


def candidate_roots(purpose: str, environ: Mapping[str, str] | None = None) -> list[Path]:
    """Ordered private roots for ``purpose``; the first eligible one wins.

    Runtime replicas are caches (``XDG_CACHE_HOME``); task checkouts hold
    work in progress (``XDG_STATE_HOME``), which cache cleaners must not
    target. The home-directory default follows a configured XDG directory,
    because tools commonly redirect XDG directories onto shared volumes. An
    explicit ``TOFU_LOCAL_DIR`` is the only candidate for both purposes.
    """
    if purpose not in MODE_KNOBS:
        raise ValueError(f'unknown placement purpose: {purpose}')
    environment = os.environ if environ is None else environ
    leaf = _ROOT_LEAVES[purpose]
    explicit = str(environment.get('TOFU_LOCAL_DIR', '') or '').strip()
    if explicit:
        return [Path(explicit).expanduser() / leaf]
    if purpose not in _DURABLE_PURPOSES:
        name, fallback = 'XDG_CACHE_HOME', ('.cache',)
    else:
        name, fallback = 'XDG_STATE_HOME', ('.local', 'state')
    home = _home(environment)
    bases = [_xdg(environment, name, fallback),
             home.joinpath(*fallback) if home is not None else None]
    return list(dict.fromkeys(base / 'tofu' / leaf for base in bases if base is not None))


def _nearest_existing(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    return candidate


def _user_id() -> int:
    getuid = getattr(os, 'getuid', None)
    return int(getuid()) if getuid is not None else 0


def ensure_private_directory(path: Path) -> None:
    """Create ``path`` 0700 or verify an existing one is privately owned."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if (not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != _user_id()
            or stat.S_IMODE(metadata.st_mode) & 0o077):
        raise PermissionError(f'placement root is not a private directory: {path}')


def reserve_bytes(total_bytes: int) -> int:
    """Free space a placement must leave on a volume of ``total_bytes``."""
    total = max(0, int(total_bytes))
    return min(total // 4, _RESERVE_CEILING_BYTES,
               max(_RESERVE_FLOOR_BYTES, int(total * _RESERVE_FRACTION)))


def has_free_inodes(path: str | os.PathLike[str], required: int = 0) -> bool:
    """Whether ``required`` more inodes fit above the reserve; unknown is not zero."""
    statvfs = getattr(os, 'statvfs', None)
    if statvfs is None:
        return True
    try:
        info = statvfs(_nearest_existing(Path(path)))
    except OSError:
        return False
    total = info.f_files
    # Several network filesystems do not report inode accounting.
    if not total:
        return True
    reserve = min(total // 4, max(128, min(8192, total // 100)))
    return info.f_favail > 0 and info.f_favail - max(0, int(required)) >= reserve


def has_free_space(
    path: str | os.PathLike[str],
    required_bytes: int = 0,
    *,
    required_inodes: int = 0,
    disk_usage: Callable[[str], object] = shutil.disk_usage,
) -> bool:
    """Whether ``required_bytes`` and ``required_inodes`` fit above both reserves."""
    try:
        usage = disk_usage(str(_nearest_existing(Path(path))))
    except OSError:
        return False
    free = int(usage.free)
    return (free > 0
            and free - max(0, int(required_bytes)) >= reserve_bytes(int(usage.total))
            and has_free_inodes(path, required_inodes))


# Age-based cleaners (systemd-tmpfiles) delete individual unread files here.
# The storage probe still calls this persistent; a placed tree is unsafe.
_AGE_CLEANED_ROOTS = ('/var/tmp',)


def _age_cleaned(path: str) -> bool:
    return any(_is_within(path, _resolved(root)) for root in _AGE_CLEANED_ROOTS)


def _persistence_admitted(purpose: str, mode: str, persistence: str) -> bool:
    if persistence == 'ephemeral':
        # Per-file cleanup would silently remove modules from a replica and
        # work from a checkout.
        return False
    if persistence == 'unknown' and purpose in _DURABLE_PURPOSES:
        # A container overlay can vanish with its container, taking any
        # uncheckpointed task work (or an unshipped authority tail) along.
        # Only an operator can accept that.
        return mode == 'on'
    return True


def _same_volume(source: Volume, target: Volume, source_path: str, target_path: str) -> bool:
    """Whether both paths share one filesystem, bind mounts included."""
    if source.device and target.device:
        return source.device == target.device
    try:
        return os.stat(source_path).st_dev == os.stat(target_path).st_dev
    except OSError:
        return False


def choose_local_root(
    source_path: str | os.PathLike[str],
    purpose: str,
    *,
    environ: Mapping[str, str] | None = None,
    required_bytes: int = 0,
    mountinfo_text: str | None = None,
    latency_probe: Callable[[str], float | None] = measure_lookup_latency,
    disk_usage: Callable[[str], object] = shutil.disk_usage,
    create: bool = True,
) -> Placement:
    """Return the first private local root that is decisively faster.

    ``auto`` selects only a source that is remote (mount table) or measured
    slow (unclassified platforms), and only a target at least
    :data:`MIN_SPEEDUP` times faster. ``on`` skips the speed comparison and,
    for task workspaces, also accepts local volumes whose persistence the
    mount table cannot prove (container overlays). Every mode refuses
    network, memory and temporary-directory targets, the source's own
    volume, non-private directories, and targets that would fall below a
    free-space reserve.

    ``create=False`` performs read-only selection; the caller owns private
    directory creation at its write/launch boundary. Reconstructible indexes
    may use noexec volumes, while runtimes and workspaces require execution.
    """
    environment = os.environ if environ is None else environ
    mode = placement_mode(purpose, environment)
    if mode == 'off':
        return Placement(False, purpose, mode, 'disabled')
    if os.name != 'posix':
        return Placement(False, purpose, mode, 'unsupported-platform')
    source = describe_volume(source_path, mountinfo_text=mountinfo_text, environ=environment)
    if mode == 'auto' and source.storage_class in LOCAL_CLASSES | {'memory-filesystem'}:
        return Placement(False, purpose, mode, 'source-local', source=source)
    source_directory = str(_nearest_existing(Path(_resolved(source_path))))
    source_latency = None
    reason = 'no-candidate'
    last_target = Volume()
    for candidate in candidate_roots(purpose, environment):
        if not candidate.is_absolute():
            reason = 'candidate-not-absolute'
            continue
        existing = str(_nearest_existing(candidate))
        if not os.access(existing, os.W_OK | os.X_OK):
            reason = 'candidate-unusable'
            continue
        target = describe_volume(existing, mountinfo_text=mountinfo_text, environ=environment)
        last_target = target
        if target.storage_class in REMOTE_CLASSES | {'memory-filesystem'}:
            reason = 'candidate-not-local'
            continue
        if purpose == PURPOSE_INDEXES and target.storage_class not in LOCAL_CLASSES:
            reason = 'candidate-not-local'
            continue
        if not target.executable and purpose != PURPOSE_INDEXES:
            # Interpreters and task scripts must run from the placed copy.
            reason = 'candidate-noexec'
            continue
        if _age_cleaned(_resolved(candidate)):
            reason = 'candidate-persistence-ephemeral'
            continue
        if not _persistence_admitted(purpose, mode, target.persistence):
            reason = f'candidate-persistence-{target.persistence}'
            continue
        if _same_volume(source, target, source_directory, existing):
            reason = 'candidate-on-source-volume'
            continue
        try:
            usage = disk_usage(existing)
        except OSError:
            reason = 'candidate-unusable'
            continue
        free = int(usage.free)
        if free <= 0 or free - max(0, int(required_bytes)) < reserve_bytes(int(usage.total)):
            reason = 'insufficient-space'
            continue
        if not has_free_inodes(existing):
            reason = 'insufficient-inodes'
            continue
        target_latency = None
        if mode == 'auto':
            if source_latency is None:
                source_latency = latency_probe(source_directory)
            target_latency = latency_probe(existing)
            if not source_latency or not target_latency:
                reason = 'latency-unmeasured'
                continue
            if source.storage_class not in REMOTE_CLASSES and source_latency < SLOW_LOOKUP_US:
                return Placement(False, purpose, mode, 'source-fast', source=source, target=target,
                                 source_latency_us=source_latency,
                                 target_latency_us=target_latency, free_bytes=free)
            if source_latency / target_latency < MIN_SPEEDUP:
                reason = 'insufficient-speedup'
                continue
        try:
            if create:
                ensure_private_directory(candidate)
            elif os.path.lexists(candidate):
                metadata = candidate.lstat()
                if (not stat.S_ISDIR(metadata.st_mode)
                        or metadata.st_uid != _user_id()
                        or stat.S_IMODE(metadata.st_mode) & 0o077):
                    raise PermissionError('index cache root is not private')
        except OSError:
            reason = 'candidate-unusable'
            continue
        return Placement(True, purpose, mode, 'selected', root=str(candidate), source=source,
                         target=target, source_latency_us=source_latency,
                         target_latency_us=target_latency, free_bytes=free)
    return Placement(False, purpose, mode, reason, source=source, target=last_target,
                     source_latency_us=source_latency)


_INDEX_ENV_KEYS = ('TOFU_INDEX_FASTPATH', 'TOFU_LOCAL_DIR', 'XDG_CACHE_HOME',
                   'HOME', 'XDG_RUNTIME_DIR')


@lru_cache(maxsize=64)
def _index_placement(source: str, environment: tuple[tuple[str, str], ...]) -> Placement:
    try:
        return choose_local_root(source, PURPOSE_INDEXES, environ=dict(environment), create=False)
    except (OSError, RuntimeError, ValueError):
        return Placement(False, PURPOSE_INDEXES, 'auto', 'candidate-unusable')


def index_cache_placement(source_path: str | os.PathLike[str], *,
                          environ: Mapping[str, str] | None = None) -> Placement:
    """Read-only, process-stable placement shared by file and database indexes.

    Only mount/metadata and capacity observations run; no directory is created
    or source data copied. Success and fallback are memoized so repeated reads
    do not benchmark the network mount, nor move an index under its writer.
    Writers still check live capacity. A new launch may choose another volume:
    these indexes are reconstructible and never hold acknowledged user data.
    """
    env = os.environ if environ is None else environ
    key = tuple((name, str(env[name])) for name in _INDEX_ENV_KEYS if name in env)
    return _index_placement(_resolved(source_path), key)


def main(argv: list[str] | None = None) -> int:
    """Print read-only placement decisions for ``SOURCE`` (default: checkout)."""
    arguments = sys.argv[1:] if argv is None else argv
    source = arguments[0] if arguments else str(Path(__file__).resolve().parent.parent)
    report = {purpose: choose_local_root(source, purpose, create=False).as_status()
              for purpose in (PURPOSE_RUNTIME, PURPOSE_NATIVE_BUILD, PURPOSE_WORKSPACES,
                              PURPOSE_INDEXES, PURPOSE_NATIVE_AUTHORITY)}
    print(json.dumps({'source': _resolved(source), **report}, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
